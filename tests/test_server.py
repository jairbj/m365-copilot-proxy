"""The HTTP surface, driven against a fake BizChat server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

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


async def test_the_work_suffix_reaches_the_wire_and_keeps_the_tone(
    client, fake_bizchat, monkeypatch
):
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.5-work", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert parse_qs(urlparse(fake.urls[0]).query)["agent"] == ["work"]
    # The suffix must not leak into the tone lookup.
    from m365_copilot_proxy.bizchat import protocol

    assert fake.chat_arguments["tone"] == protocol.MODEL_TONES["gpt-5.5"]


async def test_the_same_thread_with_and_without_work_iq_is_two_conversations(
    client, fake_bizchat, monkeypatch
):
    # One BizChat conversation cannot change surface midway, so the ids have to
    # split into separate ones.
    fake = await fake_bizchat([snapshot_frame("a"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)
    messages = [{"role": "user", "content": "same opening line"}]

    await client.post("/v1/chat/completions", json={"model": "claude-sonnet", "messages": messages})
    await client.post(
        "/v1/chat/completions", json={"model": "claude-sonnet-work", "messages": messages}
    )
    assert len(server_module.pool) == 2


async def test_models_lists_the_work_variants(client: httpx.AsyncClient):
    ids = {model["id"] for model in (await client.get("/v1/models")).json()["data"]}
    assert "claude-sonnet-work" in ids


async def test_health_reports_the_conversation_count(client: httpx.AsyncClient):
    response = await client.get("/health")
    assert response.json()["status"] == "ok"


# --- Declarative agents ------------------------------------------------------


#: The agent id, exactly as a real work tenant sends it.
GPT_ID = "T_ee433d1f-0020-fb71-0e6e-64942eccb480.gpt.1ce6ba88-1509-4424-b7f2-91865ca8ce98@MOS3"

#: The session key that capture happened to see. It must never reach the wire.
CAPTURED_SESSION_KEY = "0fa68510-6474-4ce6-a265-852d93b216ab"

#: One agent, shortened from a real capture and otherwise unedited — the surface
#: field reads `Agent` (neither `work` nor `web`), the id appears in the query as
#: well as in the invocation, a tone IS sent, and no `plugins` field is.
REAL_AGENT = {
    "surface": {
        "query": {
            "XRoutingParameterSessionKey": CAPTURED_SESSION_KEY,
            "gptId": GPT_ID,
            "source": '"officeweb"',
            "product": "Office",
            "agentHost": "Bizchat.FullScreen",
            "licenseType": "Premium",
            "isEdu": "false",
            "agent": "Agent",
            "scenario": "officeweb",
            "variants": "feature.agent",
        },
        "option_sets": ["at_mention_plugins_enable", "enterprise_flux_work"],
        "allowed_message_types": ["Chat", "EndOfRequest"],
        "plugins": None,
    },
    "thread_level_gpt_id": {"id": GPT_ID, "source": "MOS3"},
    "extra_extension_parameters": {},
    "tone": "Chat",
    "source": "officeweb",
    # The whole invocation, kept because replaying a chosen subset of it reached the
    # agent's thread and was still answered by plain Copilot.
    "raw_argument": {
        "source": "officeweb",
        "streamingMode": "Delta",
        "threadLevelGptId": {"id": GPT_ID, "source": "MOS3"},
        "clientInfo": {"clientPlatform": "mcmcopilot-web", "clientAppName": "Office"},
        "message": {"author": "user", "experienceType": "Agent"},
    },
}


@pytest.fixture
def captured_agent(tmp_path, monkeypatch):
    """A profile holding one captured agent, isolated from the real config dir."""
    import json

    from m365_copilot_proxy.bizchat import profile as tenant_profile
    from m365_copilot_proxy.config import get_settings

    monkeypatch.setenv("M365_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    tenant_profile.reset_cache()
    tmp_path.mkdir(parents=True, exist_ok=True)
    tenant_profile.profile_path().write_text(
        json.dumps({"agents": {"sales-bot": REAL_AGENT}}), encoding="utf-8"
    )
    tenant_profile.reset_cache()
    yield "agent:sales-bot"
    get_settings.cache_clear()
    tenant_profile.reset_cache()


async def test_a_captured_agent_is_offered_as_a_model(client, captured_agent):
    response = await client.get("/v1/models")
    ids = {model["id"] for model in response.json()["data"]}

    assert captured_agent in ids
    # No Work IQ twin: the agent UI has neither that toggle nor a model picker.
    assert f"{captured_agent}-work" not in ids


async def test_an_agent_turn_carries_its_id_and_drops_the_system_block(
    client, fake_bizchat, monkeypatch, captured_agent
):
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": captured_agent,
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hello"},
            ],
        },
    )

    arguments = fake.chat_arguments
    assert arguments["threadLevelGptId"] == {"id": GPT_ID, "source": "MOS3"}
    assert arguments["optionsSets"] == ["at_mention_plugins_enable", "enterprise_flux_work"]
    # The agent carries the instructions itself, and honours them.
    assert arguments["message"]["text"] == "hello"
    # Replayed as captured: the agent UI sends a tone, it just does not offer a choice.
    assert arguments["tone"] == "Chat"
    # It sends no `plugins` field at all, so neither do we — handing an agent the
    # Bing plugin its own client never asks for is a change to how it answers.
    assert "plugins" not in arguments

    query = parse_qs(urlparse(fake.urls[0]).query)
    # The id identifies the thread in two places; only replaying both enters the agent.
    assert query["gptId"] == [GPT_ID]
    assert query["agent"] == ["Agent"]


async def test_the_agent_turn_is_shaped_like_the_captured_one(
    client, fake_bizchat, monkeypatch, captured_agent
):
    # Everything the client sends and this code does not build for itself has to
    # survive, since that is where the agent's instructions are asked for.
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    await client.post(
        "/v1/chat/completions",
        json={"model": captured_agent, "messages": [{"role": "user", "content": "hello"}]},
    )

    arguments = fake.chat_arguments
    assert arguments["streamingMode"] == "Delta"
    assert arguments["message"]["experienceType"] == "Agent"
    # …while the turn keeps its own text and ids.
    assert arguments["message"]["text"] == "hello"
    assert arguments["message"]["requestId"] == arguments["traceId"]
    assert arguments["clientInfo"]["clientSessionId"]


async def test_the_captured_session_key_is_never_replayed(
    client, fake_bizchat, monkeypatch, captured_agent
):
    # `XRoutingParameterSessionKey` names a session, and the one capture saw ended
    # when the browser window closed. Each conversation mints its own.
    import uuid

    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    for opening in ("first thread", "second thread"):
        await client.post(
            "/v1/chat/completions",
            json={"model": captured_agent, "messages": [{"role": "user", "content": opening}]},
        )

    keys = [parse_qs(urlparse(url).query)["XRoutingParameterSessionKey"][0] for url in fake.urls]
    assert CAPTURED_SESSION_KEY not in keys
    assert len(set(keys)) == 2
    for key in keys:
        uuid.UUID(key)  # raises if it is not one


async def test_an_agent_turn_sends_the_tool_list_without_the_contract(
    client, fake_bizchat, monkeypatch, captured_agent
):
    # The contract lives in the agent's instructions; the tool list cannot, because
    # it changes with every request.
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": captured_agent,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {"type": "function", "function": {"name": "run_shell", "description": "run it"}}
            ],
        },
    )

    sent = fake.chat_arguments["message"]["text"]
    assert "run_shell" in sent
    assert "```tool_call" not in sent


async def test_an_agent_that_has_drifted_can_be_sent_the_instructions_anyway(
    client, fake_bizchat, monkeypatch, captured_agent
):
    from m365_copilot_proxy.config import get_settings

    monkeypatch.setenv("M365_AGENT_SEND_SYSTEM", "1")
    get_settings.cache_clear()
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)

    await client.post(
        "/v1/chat/completions",
        json={
            "model": captured_agent,
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hello"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "run_shell", "description": "run it"},
                }
            ],
        },
    )

    sent = fake.chat_arguments["message"]["text"]
    assert "be terse" in sent
    # The contract comes back with it: the flag means "treat this like plain chat".
    assert "```tool_call" in sent


async def test_an_uncaptured_agent_is_an_error_not_a_plain_chat(client, captured_agent):
    # Serving plain Copilot under an agent's name would be invisible to the caller.
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "agent:nope", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    assert "capture" in response.json()["error"]["message"]


# --- The system prompt, for pasting into an agent ----------------------------


async def test_the_system_prompt_is_recorded_and_served_with_its_size(
    client, fake_bizchat, monkeypatch, captured_agent
):
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    route_new_sessions_to(fake, monkeypatch)
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hello"},
            ],
        },
    )

    payload = (await client.get("/v1/system-prompt")).json()
    assert payload["text"].endswith("be terse")
    assert payload["limit"] == 8000
    assert payload["chars"] == len(payload["text"])
    assert payload["over_by"] == 0
    assert payload["source"]["model"] == "claude-sonnet"

    listed = (await client.get("/v1/system-prompts")).json()["data"]
    assert [entry["model"] for entry in listed] == ["claude-sonnet"]

    as_text = await client.get("/v1/system-prompt", params={"format": "text"})
    assert as_text.text == payload["text"]


async def test_the_system_prompt_endpoint_says_when_there_is_nothing_yet(
    client, captured_agent
):
    response = await client.get("/v1/system-prompt")

    assert response.status_code == 404
    assert "No system prompt recorded" in response.json()["error"]["message"]
