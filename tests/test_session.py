"""The WebSocket turn, end to end against a fake BizChat server."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from m365_copilot_proxy.bizchat.session import BizChatError, CopilotSession, TurnResult
from m365_copilot_proxy.config import get_settings
from tests.conftest import bind_session_to, make_token, snapshot_frame, stream_item, update_frame

COMPLETION = {"type": 3, "invocationId": "0"}


async def run_turn(fake, session: CopilotSession | None = None, **kwargs) -> tuple[str, TurnResult]:
    session = session or CopilotSession()
    bind_session_to(session, fake)
    result = TurnResult()
    chunks = [
        chunk
        async for chunk in session.chat(
            token=make_token(), text="hello", result=result, **kwargs
        )
    ]
    return "".join(chunks), result


async def test_a_plain_turn_streams_its_answer(fake_bizchat):
    fake = await fake_bizchat(
        [snapshot_frame("Hel"), snapshot_frame("Hello"), snapshot_frame("Hello, world"), COMPLETION]
    )
    text, result = await run_turn(fake)
    assert text == "Hello, world"
    assert result.text == "Hello, world"


async def test_the_mandatory_metrics_frame_is_sent(fake_bizchat):
    # Without it the real server completes the turn having produced nothing.
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    await run_turn(fake)
    assert fake.saw_metrics is True


async def test_write_at_cursor_deltas_are_appended(fake_bizchat):
    fake = await fake_bizchat(
        [
            update_frame(writeAtCursor="Hello", streamingMode="Delta"),
            update_frame(writeAtCursor=", "),
            update_frame(writeAtCursor="world"),
            COMPLETION,
        ]
    )
    text, _ = await run_turn(fake)
    assert text == "Hello, world"


async def test_a_snapshot_head_followed_by_deltas_loses_nothing(fake_bizchat):
    # The real server often sends the first token ONLY as a snapshot; naive delta
    # concatenation would drop it.
    fake = await fake_bizchat(
        [snapshot_frame("Hello"), update_frame(writeAtCursor=", world"), COMPLETION]
    )
    text, _ = await run_turn(fake)
    assert text == "Hello, world"


async def test_progress_messages_are_not_streamed_as_content(fake_bizchat):
    fake = await fake_bizchat(
        [
            snapshot_frame("Searching the web…", messageType="Progress"),
            snapshot_frame("The answer."),
            COMPLETION,
        ]
    )
    text, _ = await run_turn(fake)
    assert text == "The answer."


async def test_the_stream_item_finishes_the_turn_and_carries_metadata(fake_bizchat):
    fake = await fake_bizchat(
        [
            snapshot_frame("Answer"),
            stream_item(
                [{"author": "bot", "text": "Answer", "contentOrigin": "DeepLeo", "turnCount": 4}],
                turnState="Completed",
                throttling={
                    "numUserMessagesInConversation": 4,
                    "maxNumUserMessagesInConversation": 600,
                },
            ),
        ]
    )
    text, result = await run_turn(fake)
    assert text == "Answer"
    assert result.throttle == (4, 600)
    assert result.turn_state == "Completed"
    assert result.content_origin == "DeepLeo"


async def test_a_disengaged_turn_is_reported_not_silently_empty(fake_bizchat):
    fake = await fake_bizchat(
        [snapshot_frame("", messageType="Disengaged"), COMPLETION]
    )
    text, result = await run_turn(fake)
    assert text == ""
    assert result.disengaged is True


async def test_a_handshake_error_fails_loudly(fake_bizchat):
    fake = await fake_bizchat([], handshake_error="Requested protocol not supported")
    with pytest.raises(BizChatError, match="Handshake rejected"):
        await run_turn(fake)


async def test_a_completion_error_fails_loudly(fake_bizchat):
    fake = await fake_bizchat([{"type": 3, "error": "Failed to invoke 'Chat'"}])
    with pytest.raises(BizChatError, match="Failed to invoke"):
        await run_turn(fake)


async def test_ping_frames_are_answered(fake_bizchat):
    fake = await fake_bizchat([{"type": 6}, snapshot_frame("pong"), COMPLETION])
    text, _ = await run_turn(fake)
    assert text == "pong"
    assert {"type": 6} in fake.received


async def test_frames_split_across_messages_are_reassembled(fake_bizchat):
    fake = await fake_bizchat([snapshot_frame("Hello, world"), COMPLETION])
    text, _ = await run_turn(fake)
    assert text == "Hello, world"


async def test_abandoning_the_stream_cancels_the_turn(fake_bizchat):
    fake = await fake_bizchat(
        [snapshot_frame("start of a long"), snapshot_frame("start of a long answer")]
    )
    session = CopilotSession()
    bind_session_to(session, fake)

    stream = session.chat(token=make_token(), text="hello")
    assert await anext(stream) == "start of a long"
    await stream.aclose()  # the HTTP client hung up

    assert fake.saw_stop is True


async def test_the_url_carries_the_conversation_identity(fake_bizchat):
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    session = CopilotSession()
    await run_turn(fake, session)

    query = parse_qs(urlparse(fake.urls[0]).query)
    assert query["ConversationId"] == [session.conversation_id]
    assert query["X-SessionId"] == [session.session_id]
    assert query["access_token"]  # the protocol puts it here, not in a header
    assert urlparse(fake.urls[0]).path.endswith("/user-oid@tenant-id")


async def test_the_first_turn_is_marked_as_the_start_of_the_session(fake_bizchat):
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    session = CopilotSession()
    await run_turn(fake, session)
    assert fake.chat_arguments["isStartOfSession"] is True

    fake2 = await fake_bizchat([snapshot_frame("hi again"), COMPLETION])
    await run_turn(fake2, session)
    assert fake2.chat_arguments["isStartOfSession"] is False


async def test_work_iq_selects_the_surface_on_the_wire(fake_bizchat):
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    await run_turn(fake, work_iq=True)
    assert parse_qs(urlparse(fake.urls[0]).query)["agent"] == ["work"]

    fake2 = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    await run_turn(fake2, work_iq=False)
    assert parse_qs(urlparse(fake2.urls[0]).query)["agent"] == ["web"]


async def test_the_model_selects_the_tone(fake_bizchat):
    fake = await fake_bizchat([snapshot_frame("hi"), COMPLETION])
    await run_turn(fake, model="claude-sonnet")
    assert fake.chat_arguments["tone"] == "Claude_Sonnet"


async def test_image_generation_declares_its_message_type(fake_bizchat):
    # Declare-to-receive: without this the server never emits the artifact frame.
    fake = await fake_bizchat([snapshot_frame("here"), COMPLETION])
    await run_turn(fake, generate_images=True)
    assert "GenerateGraphicArt" in fake.chat_arguments["allowedMessageTypes"]
    assert "cwc_flux_image" in fake.chat_arguments["optionsSets"]


async def test_generated_images_are_captured(fake_bizchat):
    fake = await fake_bizchat(
        [
            snapshot_frame(
                "Here you go",
                contentGenerationProgressList=[
                    {"ImageReferenceUrls": ["https://example.invalid/a.png"],
                     "fileToken": "tok", "status": 1}
                ],
            ),
            snapshot_frame(
                "Here you go",
                contentGenerationProgressList=[
                    {"ImageReferenceUrls": ["https://example.invalid/a.png"],
                     "fileToken": "tok", "status": 2}
                ],
            ),
            COMPLETION,
        ]
    )
    _, result = await run_turn(fake, generate_images=True)
    # The artifact arrives repeatedly with a climbing status — one image, readiest wins.
    assert len(result.images) == 1
    assert result.images[0].status == 2


async def test_a_wss_url_connects_without_an_ssl_argument_error(monkeypatch):
    """`websockets` rejects an explicit ssl=None on wss:// as loudly as it rejects
    an ssl argument on ws://. Only omitting the keyword satisfies both, so this
    reaches a real connection attempt — against a closed local port, which fails
    immediately and never touches the network.

    The CA bundle variables are cleared deliberately: with one set there is always a
    real context to pass, which is what hid this the first time.
    """
    for name in ("M365_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    session = CopilotSession()
    session._build_url = (
        lambda token, request_id, work_iq=None: "wss://127.0.0.1:1/m365Copilot/Chathub/a@b"
    )

    with pytest.raises(OSError):  # connection refused, NOT ValueError
        async for _ in session.chat(token=make_token(), text="hello"):
            pass
