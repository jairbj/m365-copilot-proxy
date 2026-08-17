"""Rendering an OpenAI message array into one BizChat turn."""

from __future__ import annotations

from m365_copilot_proxy.openai_api.schemas import ChatMessage
from m365_copilot_proxy.openai_api.translate import build_turn_text, first_user_text


def msg(role: str, content, **extra) -> ChatMessage:
    return ChatMessage(role=role, content=content, **extra)


def test_first_user_text_skips_the_system_prompt():
    messages = [msg("system", "be terse"), msg("user", "hello")]
    assert first_user_text(messages) == "hello"


def test_multipart_content_is_flattened_to_text():
    message = msg(
        "user",
        [{"type": "text", "text": "a"}, {"type": "image_url"}, {"type": "text", "text": "b"}],
    )
    assert message.text() == "a\nb"


def test_new_conversation_with_one_message_sends_it_unlabelled():
    text = build_turn_text([msg("user", "hello")], start_index=0, is_new_conversation=True)
    # Labelling a lone opening message would prime Copilot to answer in
    # transcript form.
    assert text == "hello"


def test_new_conversation_includes_system_prompt_and_labels_the_transcript():
    messages = [
        msg("system", "be terse"),
        msg("user", "hi"),
        msg("assistant", "hello"),
        msg("user", "and now?"),
    ]
    text = build_turn_text(messages, start_index=0, is_new_conversation=True)
    assert "[System instructions]\nbe terse" in text
    assert "User: hi" in text
    assert "Assistant: hello" in text
    assert text.endswith("User: and now?")


def test_tool_instructions_are_included_on_a_new_conversation():
    text = build_turn_text(
        [msg("user", "hi")],
        start_index=0,
        is_new_conversation=True,
        tool_instructions="[Available tools]\n- x",
    )
    assert "[Available tools]" in text


def test_live_conversation_sends_only_the_new_tail():
    messages = [
        msg("user", "first"),
        msg("assistant", "answer"),
        msg("user", "second"),
    ]
    text = build_turn_text(messages, start_index=2, is_new_conversation=False)
    assert text == "second"
    assert "first" not in text


def test_live_conversation_drops_resent_system_messages():
    messages = [
        msg("user", "a"),
        msg("assistant", "b"),
        msg("system", "be terse"),
        msg("user", "c"),
    ]
    text = build_turn_text(messages, start_index=2, is_new_conversation=False)
    assert text == "c"


def test_tool_results_are_rendered_as_tool_responses():
    messages = [
        msg("user", "run ls"),
        msg("assistant", None, tool_calls=[
            {"id": "call_1", "type": "function",
             "function": {"name": "run_shell", "arguments": '{"command": "ls"}'}}
        ]),
        msg("tool", "total 0", name="run_shell", tool_call_id="call_1"),
    ]
    text = build_turn_text(messages, start_index=2, is_new_conversation=False)
    assert '<tool_response name="run_shell">' in text
    assert "total 0" in text


def test_replayed_history_includes_the_assistants_own_tool_calls():
    messages = [
        msg("user", "run ls"),
        msg("assistant", None, tool_calls=[
            {"id": "call_1", "type": "function",
             "function": {"name": "run_shell", "arguments": '{"command": "ls"}'}}
        ]),
        msg("tool", "total 0", name="run_shell", tool_call_id="call_1"),
        msg("user", "what did that show?"),
    ]
    text = build_turn_text(messages, start_index=0, is_new_conversation=True)
    # Without the call, the tool response that follows makes no sense.
    assert "```tool_call" in text
    assert "run_shell" in text


def test_empty_assistant_messages_are_skipped():
    messages = [msg("user", "a"), msg("assistant", ""), msg("user", "b")]
    text = build_turn_text(messages, start_index=0, is_new_conversation=True)
    assert "Assistant:" not in text
