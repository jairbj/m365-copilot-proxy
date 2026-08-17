"""SignalR framing and stream reconstruction."""

from __future__ import annotations

import json

from m365_copilot_proxy.bizchat import frames


def test_encode_terminates_with_the_record_separator():
    assert frames.encode({"type": 6}).endswith(frames.RS)
    assert json.loads(frames.encode({"type": 6})[:-1]) == {"type": 6}


def test_split_frames_returns_a_trailing_partial_frame_as_rest():
    complete, rest = frames.split_frames('{"a":1}\x1e{"b":2}\x1e{"c"')
    assert complete == ['{"a":1}', '{"b":2}']
    assert rest == '{"c"'


def test_split_frames_on_a_clean_boundary_leaves_no_rest():
    complete, rest = frames.split_frames('{"a":1}\x1e')
    assert complete == ['{"a":1}']
    assert rest == ""


def test_partial_frame_is_reassembled_across_chunks():
    buffer = ""
    seen: list[dict] = []
    for chunk in ['{"type":1,"tar', 'get":"update"}\x1e{"type":3}\x1e']:
        buffer += chunk
        complete, buffer = frames.split_frames(buffer)
        seen += [f for f in (frames.parse(c) for c in complete) if f]
    assert [f["type"] for f in seen] == [1, 3]
    assert buffer == ""


def test_parse_rejects_non_objects():
    assert frames.parse("[1,2]") is None
    assert frames.parse("not json") is None
    assert frames.parse("   ") is None


def test_handshake_error_reads_the_error_field():
    assert frames.handshake_error({}) is None
    assert frames.handshake_error({"error": "bad protocol"}) == "bad protocol"


def test_chat_invocation_shape():
    frame = frames.build_chat_invocation(
        text="hello",
        request_id="req-1",
        session_id="sess-1",
        is_start_of_session=True,
        tone="magic",
    )
    assert frame["type"] == 4
    assert frame["target"] == "chat"
    assert frame["invocationId"] == "0"
    args = frame["arguments"][0]
    assert args["message"]["text"] == "hello"
    assert args["message"]["author"] == "user"
    assert args["tone"] == "magic"
    assert args["isStartOfSession"] is True
    assert args["disconnectBehavior"] == "continue"


def test_metrics_frame_is_a_type_1_invocation_with_timestamps():
    frame = frames.build_metrics()
    assert frame["type"] == 1
    assert frame["target"] == "Metrics"
    assert set(frame["arguments"][0]["Timestamps"]) == {
        "ConnectionStart",
        "ConnectionEstablished",
        "UserInputStart",
        "UserInputSubmit",
    }


def test_stop_frame_uses_a_distinct_invocation_id():
    stop = frames.build_stop()
    assert (stop["type"], stop["target"], stop["invocationId"]) == (1, "stop", "1")


class TestFoldStreamText:
    def test_extension_emits_only_the_suffix(self):
        assert frames.fold_stream_text("Hel", "Hello") == ("Hello", "lo")

    def test_repeated_snapshot_emits_nothing(self):
        assert frames.fold_stream_text("Hello", "Hello") == ("Hello", None)

    def test_shorter_candidate_is_ignored(self):
        assert frames.fold_stream_text("Hello", "Hel") == ("Hello", None)

    def test_divergent_snapshot_is_adopted_but_not_emitted(self):
        # Already-streamed bytes cannot be retracted, so a rewrite updates the
        # buffered answer without emitting a non-prefix.
        assert frames.fold_stream_text("Hell", "Goodbye") == ("Goodbye", None)

    def test_every_emitted_chunk_concatenates_to_the_final_answer(self):
        answer, emitted = "", []
        # A snapshot carrying the head, then deltas — the real interleaving.
        for candidate in ["Hello", "Hello, ", "Hello, wor", "Hello, world"]:
            answer, emit = frames.fold_stream_text(answer, candidate)
            if emit:
                emitted.append(emit)
        assert "".join(emitted) == "Hello, world"


def test_message_text_ignores_control_typed_messages():
    assert frames.message_text({"text": "answer"}) == "answer"
    assert frames.message_text({"text": "thinking", "messageType": "Progress"}) is None
    assert frames.message_text({"text": "", "author": "bot"}) is None


def test_bot_messages_filters_by_author():
    argument = {"messages": [{"author": "user", "text": "hi"}, {"author": "bot", "text": "yo"}]}
    assert frames.bot_messages(argument) == [{"author": "bot", "text": "yo"}]


def test_extract_throttling():
    payload = {
        "throttling": {
            "numUserMessagesInConversation": 3,
            "maxNumUserMessagesInConversation": 600,
        }
    }
    assert frames.extract_throttling(payload) == (3, 600)
    assert frames.extract_throttling({"throttling": {}}) is None
    assert frames.extract_throttling({}) is None
