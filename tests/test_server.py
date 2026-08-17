"""The HTTP surface, driven against a fake BizChat server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from m365_copilot_proxy.bizchat import pool as pool_module
from m365_copilot_proxy.openai_api import server as server_module
from tests.conftest import make_token, snapshot_frame

COMPLETION = {"type": 3, "invocationId": "0"}


@pytest.fixture(autouse=True)
def fresh_pool():
    server_module.pool.clear()
    yield
    server_module.pool.clear()


@pytest.fixture
async def client(monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(server_module, "get_chat_token", _fake_token)
    transport = httpx.ASGITransport(app=server_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as http:
        yield http


async def _fake_token() -> str:
    return make_token()


def route_new_sessions_to(fake, monkeypatch) -> None:
    """Make every session the pool creates talk to the fake server."""
    from tests.conftest import bind_session_to

    original = pool_module.CopilotSession

    def factory(*args, **kwargs):
        session = original(*args, **kwargs)
        bind_session_to(session, fake)
        return session

    monkeypatch.setattr(pool_module, "CopilotSession", factory)


def sse_events(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload and payload != "[DONE]":
            events.append(json.loads(payload))
    return events


async def test_models_lists_the_tone_variants(client: httpx.AsyncClient):
    response = await client.get("/v1/models")
    assert response.status_code == 200
    ids = {model["id"] for model in response.json()["data"]}
    assert {"m365-copilot", "claude-sonnet", "m365-copilot-image"} <= ids


async def test_not_signed_in_returns_a_401_pointing_at_the_login_command(monkeypatch):
    from m365_copilot_proxy.auth.tokens import NeedsLoginError

    async def unauthenticated() -> str:
        raise NeedsLoginError()

    monkeypatch.setattr(server_module, "get_chat_token", unauthenticated)
    transport = httpx.ASGITransport(app=server_module.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as http:
        response = await http.post(
            "/v1/chat/completions",
            json={"model": "m365-copilot", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 401
    assert "login" in response.json()["error"]["message"]


async def test_empty_messages_is_rejected(client: httpx.AsyncClient):
    response = await client.post("/v1/chat/completions", json={"messages": []})
    assert response.status_code == 400


async def test_non_streaming_completion(client, fake_bizchat, monkeypatch):
    fake = await fake_bizchat([snapshot_frame("Hello, world"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "m365-copilot", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Hello, world"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] > 0


async def test_streaming_completion_emits_incremental_chunks(client, fake_bizchat, monkeypatch):
    fake = await fake_bizchat(
        [snapshot_frame("Hello"), snapshot_frame("Hello, world"), COMPLETION]
    )
    route_new_sessions_to(fake, monkeypatch)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-copilot",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200
    assert response.text.endswith("data: [DONE]\n\n")

    events = sse_events(response.text)
    content = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
    assert content == "Hello, world"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


async def test_a_second_turn_sends_only_the_new_message(client, fake_bizchat, monkeypatch):
    first = await fake_bizchat([snapshot_frame("one"), COMPLETION])
    route_new_sessions_to(first, monkeypatch)
    await client.post(
        "/v1/chat/completions",
        json={"model": "m365-copilot", "messages": [{"role": "user", "content": "first"}]},
    )

    # The pool reuses the existing session, so rebind that one to a fresh fake.
    second = await fake_bizchat([snapshot_frame("two"), COMPLETION])
    from tests.conftest import bind_session_to

    state = next(iter(server_module.pool._states.values()))
    bind_session_to(state.session, second)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-copilot",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "one"},
                {"role": "user", "content": "second"},
            ],
        },
    )

    sent = second.chat_arguments["message"]["text"]
    assert sent == "second"  # Copilot still remembers the rest
    assert second.chat_arguments["isStartOfSession"] is False


async def test_tool_calls_are_returned_structurally(client, fake_bizchat, monkeypatch):
    reply = '```tool_call\n{"tool": "run_shell", "arguments": {"command": "ls"}}\n```'
    fake = await fake_bizchat([snapshot_frame(reply), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "list the files"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "run_shell",
                        "description": "Run a shell command",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    },
                }
            ],
        },
    )
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "run_shell"
    assert json.loads(call["function"]["arguments"]) == {"command": "ls"}
    # The tool contract has to reach the model for any of this to work.
    assert "[Available tools]" in fake.chat_arguments["message"]["text"]


async def test_the_tools_stream_opens_before_the_answer_is_collected(
    client, fake_bizchat, monkeypatch
):
    # The tools path must read the whole answer before it can spot a tool call, but
    # an agentic client aborts a stream that stays silent (opencode's chunkTimeout),
    # so the opening chunk has to go out first.
    reply = '```tool_call\n{"tool": "run_shell", "arguments": {"command": "ls"}}\n```'
    fake = await fake_bizchat([snapshot_frame(reply), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-copilot",
            "stream": True,
            "messages": [{"role": "user", "content": "list the files"}],
            "tools": [
                {"type": "function", "function": {"name": "run_shell", "description": "run"}}
            ],
        },
    )
    events = sse_events(response.text)
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "tool_calls" not in events[0]["choices"][0]["delta"]

    call_event = next(e for e in events if e["choices"][0]["delta"].get("tool_calls"))
    assert call_event["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "run_shell"
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_an_empty_disengaged_turn_is_explained(client, fake_bizchat, monkeypatch):
    fake = await fake_bizchat([snapshot_frame("", messageType="Disengaged"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "m365-copilot", "messages": [{"role": "user", "content": "hi"}]},
    )
    content = response.json()["choices"][0]["message"]["content"]
    # A blank success is indistinguishable from a real short answer — say what happened.
    assert "Disengaged" in content


async def test_output_at_the_ceiling_reports_length(client, fake_bizchat, monkeypatch):
    monkeypatch.setenv("M365_OUTPUT_CHAR_CEILING", "10")
    from m365_copilot_proxy.config import get_settings

    get_settings.cache_clear()
    fake = await fake_bizchat([snapshot_frame("x" * 50), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "m365-copilot", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.json()["choices"][0]["finish_reason"] == "length"
    get_settings.cache_clear()


async def test_health_reports_the_conversation_count(client: httpx.AsyncClient):
    response = await client.get("/health")
    assert response.json()["status"] == "ok"
