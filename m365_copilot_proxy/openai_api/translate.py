"""Turning an OpenAI request into a BizChat turn.

BizChat takes one block of text per turn, not a message array. Two cases:

  * The conversation is new to Copilot — replay the whole history as a labelled
    transcript so it has the context the client assumes it has.
  * The conversation is live — send only the messages the client added since the
    last turn, since Copilot still remembers the rest.
"""

from __future__ import annotations

from collections.abc import Sequence

from m365_copilot_proxy.openai_api.schemas import ChatMessage
from m365_copilot_proxy.openai_api.tools import (
    render_assistant_tool_calls,
    render_tool_result,
)

SYSTEM_HEADER = "[System instructions]"


def first_user_text(messages: Sequence[ChatMessage]) -> str:
    """The text used to fingerprint the conversation."""
    for message in messages:
        if message.role == "user":
            return message.text()
    return ""


def _render_message(message: ChatMessage, *, labelled: bool) -> str:
    """One message as text. `labelled` prefixes the speaker for replayed history."""
    if message.role == "tool":
        return render_tool_result(message.name, message.text())

    if message.role == "assistant":
        parts = []
        if message.tool_calls:
            parts.append(render_assistant_tool_calls(message.tool_calls))
        text = message.text()
        if text:
            parts.append(text)
        body = "\n".join(p for p in parts if p)
        if not body:
            return ""
        return f"Assistant: {body}" if labelled else body

    text = message.text()
    if not text:
        return ""
    if message.role == "user":
        return f"User: {text}" if labelled else text
    return text


def build_turn_text(
    messages: Sequence[ChatMessage],
    *,
    start_index: int,
    is_new_conversation: bool,
    tool_instructions: str = "",
) -> str:
    """Render the text to send for this turn.

    On a new conversation everything is included: system prompts, tool contract and
    the full labelled transcript. On a live one only the tail matters, unlabelled,
    so a single user message reads as a normal message rather than a transcript
    excerpt.
    """
    if is_new_conversation:
        blocks: list[str] = []
        system_texts = [m.text() for m in messages if m.role == "system"]
        system_texts = [t for t in system_texts if t]
        if system_texts:
            blocks.append(f"{SYSTEM_HEADER}\n" + "\n\n".join(system_texts))
        if tool_instructions:
            blocks.append(tool_instructions)

        conversation = [m for m in messages if m.role != "system"]
        # A single opening user message is not a transcript — sending it labelled
        # would prime Copilot to answer in transcript form.
        labelled = len(conversation) > 1
        rendered = [_render_message(m, labelled=labelled) for m in conversation]
        rendered = [r for r in rendered if r]
        if rendered:
            blocks.append("\n\n".join(rendered))
        return "\n\n".join(blocks).strip()

    tail = list(messages)[start_index:]
    # System messages resent mid-thread are usually the client repeating itself.
    tail = [m for m in tail if m.role != "system"]
    rendered = [_render_message(m, labelled=len(tail) > 1) for m in tail]
    return "\n\n".join(r for r in rendered if r).strip()
