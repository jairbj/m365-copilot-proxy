"""SignalR JSON framing for the BizChat Chathub.

Pure functions, no I/O — which is what lets the wire format be tested against
recorded captures without opening a socket.

Protocol shape:
  * Every JSON frame is terminated by the SignalR record separator `\\x1e`, and one
    WebSocket message may carry several of them.
  * Handshake: -> {"protocol":"json","version":1}   <- {}
  * Send: a type:4 invocation to target "chat", IMMEDIATELY followed by a type:1
    invocation to target "Metrics". The Metrics frame is not telemetry politeness —
    without it the turn completes having produced no output at all.
  * Receive: type:1 target:"update" carries either an incremental `writeAtCursor`
    delta or a `messages[]` snapshot of the FULL accumulated answer; then a type:2
    stream item with the authoritative final state; then a type:3 completion.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from m365_copilot_proxy.bizchat import protocol

#: SignalR record separator (0x1e).
RS = "\x1e"

HANDSHAKE_REQUEST = {"protocol": "json", "version": 1}
PING = {"type": 6}

# SignalR message types we act on.
TYPE_INVOCATION = 1
TYPE_STREAM_ITEM = 2
TYPE_COMPLETION = 3
TYPE_CLIENT_INVOCATION = 4
TYPE_PING = 6
TYPE_CLOSE = 7


def encode(obj: Any) -> str:
    """Serialize one frame with its trailing record separator."""
    return json.dumps(obj, separators=(",", ":")) + RS


def handshake_frame() -> str:
    return encode(HANDSHAKE_REQUEST)


def ping_frame() -> str:
    return encode(PING)


def split_frames(buffer: str) -> tuple[list[str], str]:
    """Split a raw buffer into complete frames plus any trailing partial one.

    The remainder must be prepended to the next chunk: a single WebSocket message
    is not guaranteed to end on a frame boundary.
    """
    parts = buffer.split(RS)
    rest = parts.pop() if parts else ""
    return [p for p in parts if p], rest


def parse(frame: str) -> dict[str, Any] | None:
    """Parse one frame body, returning None for anything that is not a JSON object."""
    text = frame.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def handshake_error(frame: dict[str, Any] | None) -> str | None:
    """The handshake response is `{}` on success or `{"error": "..."}` on failure."""
    if not frame:
        return None
    error = frame.get("error")
    return error if isinstance(error, str) and error else None


def build_chat_invocation(
    *,
    text: str,
    request_id: str,
    session_id: str,
    is_start_of_session: bool,
    tone: str | None,
    options_sets: list[str] | None = None,
    allowed_message_types: list[str] | None = None,
    plugin_list: list[dict[str, Any]] | None = None,
    thread_level_gpt_id: dict[str, Any] | None = None,
    extra_extension_parameters: dict[str, Any] | None = None,
    source: str = "officeweb",
    locale: str = "en-us",
    time_zone: str = "UTC",
) -> dict[str, Any]:
    """The type:4 `chat` invocation carrying the user's turn.

    `thread_level_gpt_id` is what puts the turn inside a declarative agent — the
    custom agents built in the Copilot UI, which honour their own instructions where
    plain chat ignores the ones we inline. It is opaque: `capture` records the object
    the real client sends and this replays it unread. Empty means plain Copilot.

    `tone` is None for an agent, whose UI has no model picker: the field is then left
    out of the invocation entirely rather than filled with a guess.
    """
    argument: dict[str, Any] = {
        "source": source,
        "clientCorrelationId": request_id,
        "sessionId": session_id,
        "optionsSets": options_sets or [],
        "streamingMode": "ConciseWithPadding",
        "spokenTextMode": "None",
        "options": {},
        "extraExtensionParameters": dict(extra_extension_parameters or {}),
        "allowedMessageTypes": allowed_message_types or list(protocol.ALLOWED_MESSAGE_TYPES),
        "sliceIds": [],
        "threadLevelGptId": dict(thread_level_gpt_id or {}),
        "traceId": request_id,
        "isStartOfSession": is_start_of_session,
        "clientInfo": {**protocol.CLIENT_INFO, "clientSessionId": session_id},
        "message": {
            "author": "user",
            "inputMethod": "Keyboard",
            "text": text,
            "entityAnnotationTypes": list(protocol.ENTITY_ANNOTATION_TYPES),
            "requestId": request_id,
            "locationInfo": {"timeZoneOffset": 0, "timeZone": time_zone},
            "locale": locale,
            "messageType": "Chat",
            "experienceType": "Default",
            "adaptiveCards": [],
            "clientPreferences": {},
        },
        "plugins": (
            [dict(p) for p in plugin_list]
            if plugin_list is not None
            else [dict(protocol.BING_PLUGIN)]
        ),
        "isSbsSupported": True,
        "renderReferencesBehindEOS": True,
        "disconnectBehavior": "continue",
    }
    if tone is not None:
        argument["tone"] = tone
    return {
        "type": TYPE_CLIENT_INVOCATION,
        "target": "chat",
        "invocationId": "0",
        "arguments": [argument],
    }


def build_metrics() -> dict[str, Any]:
    """The mandatory companion frame to every chat invocation.

    The timestamp values themselves appear cosmetic; the frame's presence is not.
    """
    now = datetime.now(tz=UTC).isoformat()
    return {
        "type": TYPE_INVOCATION,
        "target": "Metrics",
        "arguments": [
            {
                "Timestamps": {
                    "ConnectionStart": now,
                    "ConnectionEstablished": now,
                    "UserInputStart": now,
                    "UserInputSubmit": now,
                }
            }
        ],
    }


def build_stop() -> dict[str, Any]:
    """The frame the real "Stop generating" button sends.

    A type:1 invocation on target "stop" with invocationId "1" (chat uses "0"),
    sent on the same socket; the server acks with a completion and discards the
    partial answer.
    """
    return {"type": TYPE_INVOCATION, "target": "stop", "invocationId": "1", "arguments": [{}]}


def is_update(frame: dict[str, Any] | None) -> bool:
    return bool(frame) and frame.get("type") == TYPE_INVOCATION and frame.get("target") == "update"


def update_arguments(frame: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The `arguments` payloads of an update frame, ignoring non-object entries."""
    if not is_update(frame):
        return []
    args = frame.get("arguments")  # type: ignore[union-attr]
    if not isinstance(args, list):
        return []
    return [a for a in args if isinstance(a, dict)]


def fold_stream_text(answer: str, candidate: str) -> tuple[str, str | None]:
    """Fold one piece of streamed text into the running answer.

    M365 mixes token-level deltas with full-text snapshots, and the first token
    often arrives ONLY as a snapshot — so naive delta concatenation drops the head
    of the answer. The rules, in order:

      * `candidate` no longer than the answer -> no growth, emit nothing.
      * `candidate` extends the answer -> advance and emit the appended suffix.
      * `candidate` is longer but DIVERGES -> adopt it as authoritative for the
        buffered result, but emit nothing: bytes already streamed cannot be
        retracted, so emitting a non-prefix would duplicate or corrupt the output.

    This keeps every emitted chunk a true prefix of the final answer.
    """
    if len(candidate) <= len(answer):
        return answer, None
    if candidate.startswith(answer):
        return candidate, candidate[len(answer) :]
    return candidate, None


def extract_write_at_cursor(argument: dict[str, Any]) -> str | None:
    """The incremental delta of a `streamingMode: "Delta"` update."""
    value = argument.get("writeAtCursor")
    return value if isinstance(value, str) and value else None


def bot_messages(argument: dict[str, Any]) -> list[dict[str, Any]]:
    """Bot-authored messages inside an update argument or a stream item."""
    messages = argument.get("messages")
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict) and m.get("author") == "bot"]


def message_text(message: dict[str, Any]) -> str | None:
    """The answer text of a bot message, or None when it is a control frame.

    Only messages WITHOUT a `messageType` carry content; ones with it (`Progress`,
    `Disengaged`, `EndOfRequest`, ...) are signals about the turn, not the answer.
    """
    if message.get("messageType"):
        return None
    text = message.get("text")
    return text if isinstance(text, str) and text else None


def extract_throttling(payload: dict[str, Any]) -> tuple[int, int] | None:
    """`(messages used, max)` for the conversation, when the frame reports it."""
    throttling = payload.get("throttling")
    if not isinstance(throttling, dict):
        return None
    used = throttling.get("numUserMessagesInConversation")
    total = throttling.get("maxNumUserMessagesInConversation")
    if isinstance(used, int) and isinstance(total, int):
        return used, total
    return None
