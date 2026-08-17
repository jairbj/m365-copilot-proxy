"""Emulated tool calling.

BizChat has no function-calling API: there is no field to declare tools in and no
structured call in the response. So we teach the model the contract in the prompt
and parse its answer back into OpenAI `tool_calls`.

This is heuristic by construction. Two rules keep it honest:
  * We instruct exactly ONE format (a fenced ```tool_call block) — a model given
    several formats picks unpredictably among them.
  * We PARSE more than we instruct, because the model sometimes emits a bare JSON
    object anyway. Tolerating that is free; teaching it is not.
"""

from __future__ import annotations

import json
import re
from typing import Any

from m365_copilot_proxy.openai_api.schemas import FunctionCall, ToolCall, ToolDef

#: The format we instruct and primarily parse.
_FENCED_CALL_RE = re.compile(r"```tool_call\s*\n(\{.*?\})\s*\n?```", re.DOTALL)

#: Tolerance only: a bare `{"tool": ..., "arguments": {...}}` the model emitted
#: outside the fence. Never taught, always accepted.
_BARE_CALL_RE = re.compile(
    r'\{\s*"tool"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}', re.DOTALL
)

#: Bookkeeping objects the model invents around its answer.
_CONFIDENCE_RE = re.compile(r'\{\s*"confidence"\s*:\s*-?[0-9.]+\s*\}')
_FINAL_RE = re.compile(r'\{\s*"final"\s*:\s*("(?:[^"\\]|\\.)*")\s*\}')

INSTRUCTIONS_HEADER = "[Available tools]"


def format_tool_instructions(tools: list[ToolDef]) -> str:
    """Render the tool contract that gets prepended to the conversation."""
    if not tools:
        return ""

    lines = [
        INSTRUCTIONS_HEADER,
        "You can call the tools below. To call one, reply with ONLY a fenced block:",
        "",
        "```tool_call",
        '{"tool": "tool_name", "arguments": {"arg": "value"}}',
        "```",
        "",
        "Rules:",
        "- One block per call; emit several blocks to call several tools.",
        "- Put nothing else in a reply that contains a tool call — no prose, no",
        "  explanation, no markdown around it.",
        "- `arguments` must be a JSON object matching the tool's parameters.",
        "- Never invent a tool result: stop after the call and wait for the",
        "  <tool_response> the caller sends back.",
        "- When you do not need a tool, answer normally with no fenced block.",
        "",
        "Tools:",
    ]

    for tool in tools:
        function = tool.function
        lines.append(f"- {function.name}: {function.description or 'no description'}")
        parameters = function.parameters or {}
        properties = parameters.get("properties")
        if isinstance(properties, dict) and properties:
            required = set(parameters.get("required") or [])
            for name, spec in properties.items():
                spec = spec if isinstance(spec, dict) else {}
                kind = spec.get("type", "any")
                flag = "required" if name in required else "optional"
                description = spec.get("description", "")
                suffix = f" — {description}" if description else ""
                lines.append(f"    - {name} ({kind}, {flag}){suffix}")
    return "\n".join(lines)


def _parse_call(payload: str) -> ToolCall | None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("tool") or data.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = data.get("arguments", {})
    if isinstance(arguments, str):
        # Some replies double-encode the arguments object.
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"input": arguments}
    if not isinstance(arguments, dict):
        arguments = {}
    return ToolCall(function=FunctionCall(name=name, arguments=json.dumps(arguments)))


def clean_loose_text(text: str) -> str:
    """Drop the bookkeeping objects the model wraps around a plain answer."""
    out = _FINAL_RE.sub(lambda m: json.loads(m.group(1)), text)
    out = _CONFIDENCE_RE.sub("", out)
    return out.strip()


def parse_tool_calls(text: str) -> tuple[list[ToolCall], str]:
    """Split a reply into tool calls and whatever prose surrounds them.

    Returns `(calls, remaining_text)`. `remaining_text` is what should be shown as
    content; it is empty for a well-behaved tool-calling reply.
    """
    calls: list[ToolCall] = []
    remainder = text

    def take(match: re.Match[str], payload: str) -> str:
        call = _parse_call(payload)
        if call is None:
            return match.group(0)  # not a call after all — leave the text alone
        calls.append(call)
        return ""

    remainder = _FENCED_CALL_RE.sub(lambda m: take(m, m.group(1)), remainder)
    if not calls:
        remainder = _BARE_CALL_RE.sub(lambda m: take(m, m.group(0)), remainder)

    remainder = clean_loose_text(remainder)
    if calls:
        # Prose alongside a tool call is almost always a premature claim about
        # what the call will return, so it is dropped rather than shown.
        remainder = ""
    return calls, remainder


def render_tool_result(name: str | None, content: str) -> str:
    """Render a tool result the way the instructions promise it will arrive."""
    label = f' name="{name}"' if name else ""
    return f"<tool_response{label}>\n{content}\n</tool_response>"


def render_assistant_tool_calls(tool_calls: list[Any]) -> str:
    """Replay an assistant turn's tool calls back into the transcript.

    Needed when a conversation is replayed from scratch (new server-side
    conversation): Copilot must see its own earlier calls to make sense of the
    tool responses that follow.
    """
    blocks: list[str] = []
    for call in tool_calls:
        function = getattr(call, "function", None)
        name = getattr(function, "name", None) or "unknown"
        arguments = getattr(function, "arguments", None) or "{}"
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = {"raw": arguments}
        payload = json.dumps({"tool": name, "arguments": parsed})
        blocks.append(f"```tool_call\n{payload}\n```")
    return "\n".join(blocks)
