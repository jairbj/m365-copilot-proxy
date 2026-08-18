"""A conversation with M365 Copilot over the BizChat Chathub.

One `CopilotSession` is one server-side conversation: the session and conversation
ids are reused across turns so Copilot keeps its own context, while each turn opens
a fresh WebSocket (that is how the real web client behaves too).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from m365_copilot_proxy import tls
from m365_copilot_proxy.auth.tokens import decode_jwt, redact
from m365_copilot_proxy.bizchat import frames, protocol
from m365_copilot_proxy.bizchat.images import GeneratedImage, capture_images
from m365_copilot_proxy.bizchat.profile import DeclarativeAgent
from m365_copilot_proxy.config import get_settings

log = logging.getLogger(__name__)

#: A piece of streamed text: an incremental `writeAtCursor` delta to append, or a
#: full-text snapshot of the answer so far. The server emits both, interleaved.
TextPiece = tuple[Literal["delta", "snapshot"], str]


class BizChatError(RuntimeError):
    """The server refused or aborted the turn."""


@dataclass
class TurnResult:
    """Everything a turn produced besides the streamed text itself.

    Read it after the stream is exhausted; the fields are filled as frames arrive.
    """

    text: str = ""
    images: list[GeneratedImage] = field(default_factory=list)
    #: `(messages used, max)` in this conversation, straight from the server.
    throttle: tuple[int, int] | None = None
    #: Last `messageType` seen on a bot message — `Disengaged` means the safety
    #: layer stopped the turn, which is otherwise indistinguishable from an
    #: ordinary short answer.
    message_type: str | None = None
    content_origin: str | None = None
    turn_state: str | None = None
    #: Authoritative server-side turn counter for the conversation.
    turn_count: int | None = None
    #: Highest classifier score per component across the turn.
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def disengaged(self) -> bool:
        return self.message_type == "Disengaged"


class CopilotSession:
    """One BizChat conversation. Not safe for concurrent turns."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.conversation_id = conversation_id or str(uuid.uuid4())
        self.turn_count = 0
        #: Server-reported usage of the 600-message conversation budget.
        self.messages_used = 0

    def reset_conversation(self) -> None:
        """Start a brand-new server-side conversation, keeping this object."""
        self.conversation_id = str(uuid.uuid4())
        self.turn_count = 0
        self.messages_used = 0
        log.info("Rotated to a new conversation: cid=%s", self.conversation_id)

    def _build_url(
        self,
        token: str,
        request_id: str,
        work_iq: bool | None = None,
        agent: DeclarativeAgent | None = None,
    ) -> str:
        claims = decode_jwt(token)
        query = {
            "chatsessionid": request_id,
            "clientrequestid": request_id,
            "X-SessionId": self.session_id,
            "ConversationId": self.conversation_id,
            "access_token": token,
            "variants": protocol.variants(work_iq, agent),
            **protocol.query_defaults(work_iq, agent),
        }
        return (
            f"wss://{protocol.WS_HOST}{protocol.WS_PATH}/"
            f"{claims.chathub_path}?{urlencode(query)}"
        )

    async def chat(
        self,
        *,
        token: str,
        text: str,
        model: str = protocol.DEFAULT_MODEL,
        generate_images: bool = False,
        work_iq: bool | None = None,
        agent: DeclarativeAgent | None = None,
        result: TurnResult | None = None,
    ) -> AsyncIterator[str]:
        """Send one turn and yield the answer incrementally.

        Pass a `TurnResult` in to read the turn's metadata afterwards — an async
        generator cannot return a value to its consumer.

        `agent` puts the turn inside a captured declarative agent, which replaces the
        whole surface: its own connection fields, optionsSets, plugins and tone.
        """
        settings = get_settings()
        result = result if result is not None else TurnResult()
        request_id = str(uuid.uuid4())
        is_first = self.turn_count == 0
        self.turn_count += 1

        options_sets = protocol.option_sets(
            work_iq=work_iq, generate_images=generate_images, agent=agent
        )
        allowed = protocol.allowed_message_types(
            work_iq=work_iq, generate_images=generate_images, agent=agent
        )
        plugin_list = protocol.plugins(work_iq, agent)

        url = self._build_url(token, request_id, work_iq, agent)
        # The surface is in the log because "why did this answer not find my email"
        # is exactly the question it answers.
        log.info(
            "Turn %d: model=%s surface=%s cid=%s images=%s",
            self.turn_count,
            model,
            protocol.query_defaults(work_iq, agent).get("agent"),
            self.conversation_id,
            generate_images,
        )
        log.debug("Connecting to %s", redact(url))

        images: dict[str, GeneratedImage] = {}
        #: The single authoritative reconstruction of the answer.
        answer = ""
        handshake_done = False
        completed = False

        try:
            async with connect(
                url,
                origin=protocol.ORIGIN,  # type: ignore[arg-type]
                user_agent_header=protocol.USER_AGENT,
                additional_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                max_size=None,
                open_timeout=30,
                **tls.websocket_ssl_kwargs(url),
            ) as ws:
                try:
                    await ws.send(frames.handshake_frame())
                    buffer = ""
                    while not completed:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=settings.turn_timeout)
                        except TimeoutError as exc:
                            raise BizChatError(
                                f"No response from Copilot for {settings.turn_timeout:.0f}s"
                            ) from exc
                        except ConnectionClosed as exc:
                            # The server hangs up once the turn is done; only a close
                            # before any content is a real failure.
                            if answer or result.message_type:
                                log.debug("Connection closed after the answer: %s", exc)
                                break
                            raise BizChatError(
                                "Copilot closed the connection before answering "
                                f"({exc.__class__.__name__})"
                            ) from exc

                        buffer += raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                        complete, buffer = frames.split_frames(buffer)

                        for chunk in complete:
                            frame = frames.parse(chunk)
                            self._dump(request_id, "recv", frame if frame is not None else chunk)

                            if not handshake_done:
                                handshake_done = True
                                error = frames.handshake_error(frame)
                                if error:
                                    raise BizChatError(f"Handshake rejected: {error}")
                                await self._send_turn(
                                    ws,
                                    request_id=request_id,
                                    text=text,
                                    model=model,
                                    is_first=is_first,
                                    options_sets=options_sets,
                                    allowed=allowed,
                                    plugin_list=plugin_list,
                                    agent=agent,
                                )
                                continue

                            if frame is None:
                                continue

                            kind = frame.get("type")
                            if kind == frames.TYPE_PING:
                                await ws.send(frames.ping_frame())
                                continue

                            if kind == frames.TYPE_INVOCATION:
                                pieces = self._handle_update(frame, result, images)
                            elif kind == frames.TYPE_STREAM_ITEM:
                                pieces = self._handle_stream_item(frame, result, images)
                                completed = True
                            elif kind == frames.TYPE_COMPLETION:
                                pieces = []
                                error = frame.get("error")
                                if isinstance(error, str) and error:
                                    raise BizChatError(f"Copilot rejected the turn: {error}")
                                completed = True
                            elif kind == frames.TYPE_CLOSE:
                                pieces = []
                                error = frame.get("error")
                                if isinstance(error, str) and error:
                                    raise BizChatError(f"Copilot closed the connection: {error}")
                                completed = True
                            else:
                                continue

                            for kind_of_piece, piece in pieces:
                                candidate = answer + piece if kind_of_piece == "delta" else piece
                                answer, emit = frames.fold_stream_text(answer, candidate)
                                if emit:
                                    yield emit

                            if completed:
                                break
                finally:
                    # Reaching here without the turn having completed means the caller
                    # walked away (HTTP client disconnected) or something failed. Cancel
                    # the turn the way the real "Stop generating" button does, so the
                    # server stops generating an answer nobody will read.
                    if not completed and handshake_done:
                        await self._send_stop(ws, request_id)

        except Exception as exc:
            # The WebSocket verifies certificates with its own stack, so it can
            # fail here even when MSAL's HTTPS calls went through. Anything that
            # is not a certificate problem passes through untouched.
            explanation = tls.explain_ssl_error(exc, protocol.WS_HOST)
            if explanation:
                raise BizChatError(explanation) from exc
            raise

        result.text = answer
        result.images = list(images.values())
        if result.throttle:
            self.messages_used = result.throttle[0]
        elif result.turn_count is not None:
            self.messages_used = result.turn_count
        log.info(
            "Turn finished: %d chars, throttle=%s, messageType=%s",
            len(answer),
            result.throttle,
            result.message_type,
        )

    async def _send_turn(
        self,
        ws: ClientConnection,
        *,
        request_id: str,
        text: str,
        model: str,
        is_first: bool,
        options_sets: list[str],
        allowed: list[str],
        plugin_list: list[dict[str, Any]] | None = None,
        agent: DeclarativeAgent | None = None,
    ) -> None:
        """Send the chat invocation and its mandatory Metrics companion."""
        chat = frames.build_chat_invocation(
            text=text,
            request_id=request_id,
            session_id=self.session_id,
            is_start_of_session=is_first,
            tone=protocol.tone_for_model(model),
            options_sets=options_sets,
            allowed_message_types=allowed,
            plugin_list=plugin_list,
            thread_level_gpt_id=agent.thread_level_gpt_id if agent else None,
            extra_extension_parameters=agent.extra_extension_parameters if agent else None,
            source=(agent.source if agent and agent.source else "officeweb"),
        )
        metrics = frames.build_metrics()
        self._dump(request_id, "send", chat)
        self._dump(request_id, "send", metrics)
        # One send, both frames: the server treats the Metrics frame as part of the
        # turn, and a turn without it silently produces nothing.
        await ws.send(frames.encode(chat) + frames.encode(metrics))

    async def _send_stop(self, ws: ClientConnection, request_id: str) -> None:
        try:
            log.info("Cancelling the in-flight turn (stop frame)")
            stop = frames.build_stop()
            self._dump(request_id, "send", stop)
            await ws.send(frames.encode(stop))
        except Exception as exc:
            log.debug("Could not send the stop frame: %s", exc)

    def _handle_update(
        self,
        frame: dict[str, Any],
        result: TurnResult,
        images: dict[str, GeneratedImage],
    ) -> list[TextPiece]:
        """Text pieces carried by a type:1 update frame."""
        pieces: list[TextPiece] = []
        for argument in frames.update_arguments(frame):
            throttle = frames.extract_throttling(argument)
            if throttle:
                result.throttle = throttle

            delta = frames.extract_write_at_cursor(argument)
            if delta is not None:
                pieces.append(("delta", delta))
                continue

            for message in frames.bot_messages(argument):
                self._absorb_metadata(message, result, images)
                text = frames.message_text(message)
                if text:
                    pieces.append(("snapshot", text))
        return pieces

    def _handle_stream_item(
        self,
        frame: dict[str, Any],
        result: TurnResult,
        images: dict[str, GeneratedImage],
    ) -> list[TextPiece]:
        """The type:2 item: the authoritative final state of the turn."""
        item = frame.get("item")
        if not isinstance(item, dict):
            return []
        if isinstance(item.get("turnState"), str):
            result.turn_state = item["turnState"]
        throttle = frames.extract_throttling(item)
        if throttle:
            result.throttle = throttle

        pieces: list[TextPiece] = []
        for message in frames.bot_messages(item):
            self._absorb_metadata(message, result, images)
            text = frames.message_text(message)
            if text:
                pieces.append(("snapshot", text))
        return pieces

    @staticmethod
    def _absorb_metadata(
        message: dict[str, Any],
        result: TurnResult,
        images: dict[str, GeneratedImage],
    ) -> None:
        """Pull diagnostics off any bot message, control-typed ones included."""
        capture_images(message, images)
        if isinstance(message.get("contentOrigin"), str):
            result.content_origin = message["contentOrigin"]
        if isinstance(message.get("messageType"), str):
            result.message_type = message["messageType"]
        if isinstance(message.get("turnCount"), int):
            result.turn_count = message["turnCount"]
        if isinstance(message.get("turnState"), str):
            result.turn_state = message["turnState"]
        scores = message.get("scores")
        if isinstance(scores, list):
            for score in scores:
                if not isinstance(score, dict):
                    continue
                component, value = score.get("component"), score.get("score")
                if isinstance(component, str) and isinstance(value, int | float):
                    # The worst score across the turn is the informative one.
                    result.scores[component] = max(result.scores.get(component, 0.0), value)

    @staticmethod
    def _dump(request_id: str, direction: str, frame: Any) -> None:
        """Append a frame to a per-request NDJSON file (debugging only).

        Frames carry answer text but never the token — the token lives in the URL —
        so this is safe to hand over when reporting a protocol change.
        """
        settings = get_settings()
        if not settings.dump_frames:
            return
        try:
            settings.frames_dir.mkdir(parents=True, exist_ok=True)
            path = settings.frames_dir / f"{request_id}.ndjson"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"dir": direction, "frame": frame}) + "\n")
        except Exception as exc:
            log.debug("Could not dump frame: %s", exc)
