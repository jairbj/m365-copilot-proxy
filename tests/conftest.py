"""Shared fixtures: a fake BizChat server and a fake token."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from m365_copilot_proxy.bizchat import frames


def make_token(oid: str = "user-oid", tid: str = "tenant-id", ttl: int = 3600) -> str:
    """A JWT-shaped token. Only the payload is ever read, and never verified."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims = {
        "oid": oid,
        "tid": tid,
        "exp": int(time.time()) + ttl,
        "aud": "https://substrate.office.com/sydney",
        "upn": "someone@example.com",
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


@dataclass
class FakeBizChat:
    """A BizChat stand-in that records what the client sent and replays a script."""

    #: Frames the server sends after the chat invocation arrives.
    script: list[dict[str, Any]] = field(default_factory=list)
    #: One script per chat invocation, for turns that must answer differently the
    #: second time — an empty answer followed by a real one, say. Falls back to
    #: `script` once exhausted, so tests that do not care never notice.
    scripts: list[list[dict[str, Any]]] = field(default_factory=list)
    received: list[dict[str, Any]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    #: Set when the client sends the mandatory Metrics frame.
    saw_metrics: bool = False
    saw_stop: bool = False
    handshake_error: str | None = None
    port: int = 0

    @property
    def chat_arguments(self) -> dict[str, Any]:
        for frame in self.received:
            if frame.get("target") == "chat":
                return frame["arguments"][0]
        raise AssertionError("no chat invocation was received")

    async def handler(self, connection: ServerConnection) -> None:
        self.urls.append(connection.request.path)
        buffer = ""
        handshake_done = False
        async for message in connection:
            buffer += message if isinstance(message, str) else message.decode()
            complete, buffer = frames.split_frames(buffer)
            for chunk in complete:
                frame = frames.parse(chunk)
                if frame is None:
                    continue
                if not handshake_done:
                    handshake_done = True
                    if self.handshake_error:
                        await connection.send(frames.encode({"error": self.handshake_error}))
                        await connection.close()
                        return
                    await connection.send(frames.encode({}))
                    continue

                self.received.append(frame)
                if frame.get("target") == "Metrics":
                    self.saw_metrics = True
                    continue
                if frame.get("target") == "stop":
                    self.saw_stop = True
                    await connection.send(frames.encode({"type": 3, "invocationId": "1"}))
                    continue
                if frame.get("target") == "chat":
                    turn = len([f for f in self.received if f.get("target") == "chat"]) - 1
                    outgoing_frames = (
                        self.scripts[turn] if turn < len(self.scripts) else self.script
                    )
                    for outgoing in outgoing_frames:
                        await connection.send(frames.encode(outgoing))


@pytest.fixture
async def fake_bizchat() -> AsyncIterator[Callable[..., Any]]:
    """Start a fake server and hand back a factory that points a session at it."""
    servers: list[Any] = []

    async def start(script: list[dict[str, Any]], **kwargs: Any) -> FakeBizChat:
        fake = FakeBizChat(script=script, **kwargs)
        server = await serve(fake.handler, "127.0.0.1", 0)
        servers.append(server)
        fake.port = server.sockets[0].getsockname()[1]
        return fake

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


def bind_session_to(session: Any, fake: FakeBizChat) -> None:
    """Rewrite the session's URL builder to reach the fake server over plain ws.

    Always builds from the unpatched class method, so re-binding a session to a
    second fake server replaces the first target instead of stacking on it.
    """
    from m365_copilot_proxy.bizchat.session import CopilotSession

    def build(*args: Any, **kwargs: Any) -> str:
        url = CopilotSession._build_url(session, *args, **kwargs)
        return url.replace("wss://substrate.office.com", f"ws://127.0.0.1:{fake.port}")

    session._build_url = build  # type: ignore[method-assign]


def update_frame(**payload: Any) -> dict[str, Any]:
    return {"type": 1, "target": "update", "arguments": [payload]}


def snapshot_frame(text: str, **extra: Any) -> dict[str, Any]:
    return update_frame(messages=[{"author": "bot", "text": text, **extra}])


def stream_item(messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"type": 2, "invocationId": "0", "item": {"messages": messages, **extra}}


__all__ = [
    "FakeBizChat",
    "bind_session_to",
    "make_token",
    "snapshot_frame",
    "stream_item",
    "update_frame",
    "websockets",
]
