"""The OpenAI-compatible HTTP server."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from m365_copilot_proxy.auth.tokens import (
    NeedsLoginError,
    account_summary,
    decode_jwt,
    get_chat_token,
)
from m365_copilot_proxy.bizchat import protocol
from m365_copilot_proxy.bizchat.images import render_images_markdown
from m365_copilot_proxy.bizchat.pool import PooledTurn, SessionPool, conversation_key
from m365_copilot_proxy.bizchat.session import BizChatError, TurnResult
from m365_copilot_proxy.config import get_settings
from m365_copilot_proxy.openai_api.schemas import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    Choice,
    ChunkChoice,
    Delta,
    ModelCard,
    ModelList,
    ResponseMessage,
    ToolCall,
    Usage,
    estimate_tokens,
)
from m365_copilot_proxy.openai_api.tools import format_tool_instructions, parse_tool_calls
from m365_copilot_proxy.openai_api.translate import build_turn_text, first_user_text

log = logging.getLogger(__name__)

pool = SessionPool()


def create_app() -> FastAPI:
    app = FastAPI(title="m365-copilot-proxy", version="0.1.0")

    @app.exception_handler(NeedsLoginError)
    async def _needs_login(_: Request, exc: NeedsLoginError) -> JSONResponse:
        return error_response(str(exc), status=401, error_type="authentication_error")

    @app.exception_handler(BizChatError)
    async def _bizchat_error(_: Request, exc: BizChatError) -> JSONResponse:
        return error_response(str(exc), status=502, error_type="upstream_error")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "conversations": str(len(pool))}

    @app.get("/v1/auth/status")
    async def auth_status() -> JSONResponse:
        account = account_summary()
        if account is None:
            return JSONResponse(
                {"signed_in": False, "hint": "Run `m365-copilot-proxy login`."}
            )
        payload: dict[str, object] = {"signed_in": True, **account}
        try:
            claims = decode_jwt(await get_chat_token())
            payload["token_expires_at"] = claims.expires_at.isoformat()
            payload["token_expires_in_seconds"] = int(claims.seconds_remaining)
            payload["audience"] = claims.audience
        except Exception as exc:
            payload["signed_in"] = False
            payload["error"] = str(exc)
        return JSONResponse(payload)

    @app.get("/v1/models")
    async def list_models() -> ModelList:
        return ModelList(data=[ModelCard(id=name) for name in protocol.available_models()])

    # response_model=None: one route legitimately returns three shapes — a
    # completion, an SSE stream and an error — so FastAPI must not infer one.
    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(body: ChatCompletionRequest) -> object:
        if not body.messages:
            return error_response("`messages` must not be empty", status=400)
        return await run_completion(body)

    return app


def error_response(
    message: str, *, status: int = 500, error_type: str = "invalid_request_error"
) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": error_type, "code": None}}, status_code=status
    )


def finish_reason_for(text: str, has_tool_calls: bool) -> str:
    """`tool_calls`, or `length` when the answer hit M365's soft output ceiling.

    M365 caps output around ~3k tokens and CONCLUDES EARLY rather than truncating,
    so an over-long answer comes back looking clean but incomplete, with nothing to
    detect. Reporting `length` gives harnesses the standard cue to ask for a
    continuation.
    """
    if has_tool_calls:
        return "tool_calls"
    ceiling = get_settings().output_char_ceiling
    if ceiling > 0 and len(text) >= ceiling:
        log.info("Answer reached the %d-char ceiling — reporting finish_reason=length", ceiling)
        return "length"
    return "stop"


async def run_completion(body: ChatCompletionRequest) -> object:
    settings = get_settings()
    token = await get_chat_token()

    key = conversation_key(body.model, first_user_text(body.messages))
    generate_images = body.model == protocol.IMAGE_MODEL or settings.images_always
    # With tools declared we cannot stream: a tool call is only recognisable once
    # the fenced block is complete, and half a block must never reach the client.
    buffered = bool(body.tools)

    # Held for the whole turn so two requests never run on one session. On the
    # streaming path ownership passes to the response generator, which outlives
    # this function — hence the manual acquire/release rather than `async with`.
    lock = pool.lock_for(key)
    await lock.acquire()
    lock_transferred = False
    try:
        turn = pool.acquire(key, len(body.messages))
        text = build_turn_text(
            body.messages,
            start_index=turn.start_index,
            is_new_conversation=turn.is_new,
            tool_instructions=format_tool_instructions(body.tools or []),
        )
        if not text.strip():
            return error_response("No new message content to send", status=400)

        result = TurnResult()
        stream = turn.session.chat(
            token=token,
            text=text,
            model=body.model,
            generate_images=generate_images,
            result=result,
        )

        if body.stream:
            inner = (
                buffered_stream(stream, result, body, turn, len(body.messages))
                if buffered
                else stream_completion(stream, result, body, turn, len(body.messages))
            )
            lock_transferred = True
            return StreamingResponse(
                _releasing(inner, lock),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        answer = "".join([chunk async for chunk in stream])
        answer = await _append_images(answer, result)
        calls, remainder = parse_tool_calls(answer) if body.tools else ([], answer)
        turn.commit(len(body.messages) + 1)
        return completion_response(remainder, calls, body, result, text)
    finally:
        if not lock_transferred:
            lock.release()


async def _releasing(inner: AsyncIterator[str], lock: asyncio.Lock) -> AsyncIterator[str]:
    """Forward a stream, releasing the conversation lock once it is finished.

    `finally` also covers the client hanging up mid-stream: FastAPI closes the
    generator, which unwinds this and the underlying WebSocket turn.
    """
    try:
        async for chunk in inner:
            yield chunk
    finally:
        lock.release()


async def _append_images(answer: str, result: TurnResult) -> str:
    if not result.images:
        return answer
    markdown = await render_images_markdown(result.images)
    if not markdown:
        return answer
    return f"{answer}\n\n{markdown}".strip()


def _chunk(model: str, completion_id: str, delta: Delta, finish: str | None = None) -> str:
    chunk = ChatCompletionChunk(
        id=completion_id,
        model=model,
        choices=[ChunkChoice(delta=delta, finish_reason=finish)],
    )
    return f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"


def _empty_answer_note(result: TurnResult) -> str:
    """Explain an empty answer instead of returning a blank, successful-looking one.

    An empty reply is indistinguishable from a genuine short one, so the two known
    causes get named: the safety layer stopping the turn, and everything else.
    """
    if result.disengaged:
        return (
            "_(Microsoft 365 Copilot declined to answer this turn — its safety "
            "layer marked the conversation Disengaged.)_"
        )
    return (
        "_(Microsoft 365 Copilot returned no content for this turn. "
        f"messageType={result.message_type}, turnState={result.turn_state})_"
    )


async def stream_completion(
    stream: AsyncIterator[str],
    result: TurnResult,
    body: ChatCompletionRequest,
    turn: PooledTurn,
    message_count: int,
) -> AsyncIterator[str]:
    """SSE for the plain (no tools) path: forward deltas as they arrive."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    yield _chunk(body.model, completion_id, Delta(role="assistant", content=""))

    answer = ""
    try:
        async for delta in stream:
            answer += delta
            yield _chunk(body.model, completion_id, Delta(content=delta))
    except BizChatError as exc:
        log.warning("Turn failed mid-stream: %s", exc)
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    extra = await _append_images("", result)
    if extra:
        yield _chunk(body.model, completion_id, Delta(content=f"\n\n{extra}"))
        answer += f"\n\n{extra}"
    if not answer.strip():
        note = _empty_answer_note(result)
        yield _chunk(body.model, completion_id, Delta(content=note))
        answer = note

    yield _chunk(body.model, completion_id, Delta(), finish_reason_for(answer, False))
    yield "data: [DONE]\n\n"
    # Only a turn that ran to completion counts as delivered: if the client hung up
    # mid-stream this never runs, and the messages are resent next time.
    turn.commit(message_count + 1)


async def buffered_stream(
    stream: AsyncIterator[str],
    result: TurnResult,
    body: ChatCompletionRequest,
    turn: PooledTurn,
    message_count: int,
) -> AsyncIterator[str]:
    """SSE for the tools path, where the whole answer must be read before emitting.

    A tool call is only recognisable once its fenced block is complete, and half a
    block must never reach the client. But the opening chunk is sent BEFORE the turn
    is collected: agentic clients abort a stream that goes quiet for too long
    (opencode's `chunkTimeout`), and a reasoning model easily takes a minute.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    yield _chunk(body.model, completion_id, Delta(role="assistant", content=""))

    try:
        answer = "".join([chunk async for chunk in stream])
    except BizChatError as exc:
        log.warning("Turn failed: %s", exc)
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    answer = await _append_images(answer, result)
    calls, text = parse_tool_calls(answer) if body.tools else ([], answer)

    if calls:
        tool_deltas = [
            {
                "index": index,
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for index, call in enumerate(calls)
        ]
        yield _chunk(body.model, completion_id, Delta(tool_calls=tool_deltas))
    else:
        if not text.strip():
            text = _empty_answer_note(result)
        yield _chunk(body.model, completion_id, Delta(content=text))

    yield _chunk(body.model, completion_id, Delta(), finish_reason_for(text, bool(calls)))
    yield "data: [DONE]\n\n"
    turn.commit(message_count + 1)


def completion_response(
    text: str,
    calls: list[ToolCall],
    body: ChatCompletionRequest,
    result: TurnResult,
    prompt: str,
) -> ChatCompletion:
    if not calls and not text.strip():
        text = _empty_answer_note(result)
    message = ResponseMessage(
        content=text or None if calls else text,
        tool_calls=calls or None,
    )
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(text)
    return ChatCompletion(
        model=body.model,
        choices=[Choice(message=message, finish_reason=finish_reason_for(text, bool(calls)))],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


app = create_app()


def run(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        app,
        host=host or settings.host,
        port=port or settings.port,
        log_level=settings.log_level.lower(),
    )


__all__ = ["app", "create_app", "run"]
