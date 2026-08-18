"""Emulated tool calling: instruction rendering and reply parsing."""

from __future__ import annotations

import json

from m365_copilot_proxy.openai_api.schemas import FunctionDef, ToolDef
from m365_copilot_proxy.openai_api.tools import (
    TOOL_CONTRACT,
    clean_loose_text,
    format_tool_instructions,
    parse_tool_calls,
    render_assistant_tool_calls,
    render_tool_result,
)

SHELL_TOOL = ToolDef(
    function=FunctionDef(
        name="run_shell",
        description="Run a shell command",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command"},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
        },
    )
)


def test_no_tools_means_no_instructions():
    assert format_tool_instructions([]) == ""


def test_instructions_describe_each_parameter():
    text = format_tool_instructions([SHELL_TOOL])
    assert "run_shell: Run a shell command" in text
    assert "command (string, required)" in text
    assert "timeout (integer, optional)" in text


def test_fenced_call_is_parsed():
    calls, remainder = parse_tool_calls(
        '```tool_call\n{"tool": "run_shell", "arguments": {"command": "ls -la"}}\n```'
    )
    assert len(calls) == 1
    assert calls[0].function.name == "run_shell"
    assert json.loads(calls[0].function.arguments) == {"command": "ls -la"}
    assert remainder == ""


def test_several_calls_in_one_reply():
    calls, _ = parse_tool_calls(
        '```tool_call\n{"tool": "a", "arguments": {}}\n```\n'
        '```tool_call\n{"tool": "b", "arguments": {"x": 1}}\n```'
    )
    assert [c.function.name for c in calls] == ["a", "b"]


def test_bare_json_call_outside_the_fence_is_tolerated():
    calls, remainder = parse_tool_calls(
        'Sure, running it. {"tool": "run_shell", "arguments": {"command": "pwd"}}'
    )
    assert len(calls) == 1
    # Prose alongside a call is a premature claim about the result — dropped.
    assert remainder == ""


def test_double_encoded_arguments_are_unwrapped():
    calls, _ = parse_tool_calls(
        '```tool_call\n{"tool": "run_shell", "arguments": "{\\"command\\": \\"id\\"}"}\n```'
    )
    assert json.loads(calls[0].function.arguments) == {"command": "id"}


def test_a_fenced_block_that_is_not_a_call_is_left_as_text():
    reply = '```tool_call\n{"not": "a call"}\n```'
    calls, remainder = parse_tool_calls(reply)
    assert calls == []
    assert "not" in remainder


def test_plain_answer_passes_through():
    calls, remainder = parse_tool_calls("The answer is 42.")
    assert calls == []
    assert remainder == "The answer is 42."


def test_invented_bookkeeping_objects_are_stripped():
    assert clean_loose_text('Answer.{"confidence": 0.9}') == "Answer."
    assert clean_loose_text('{"final": "Done."}') == "Done."


def test_tool_result_rendering_matches_the_promised_shape():
    rendered = render_tool_result("run_shell", "total 0")
    assert rendered.startswith('<tool_response name="run_shell">')
    assert rendered.endswith("</tool_response>")


def test_assistant_calls_replay_as_fenced_blocks():
    calls, _ = parse_tool_calls(
        '```tool_call\n{"tool": "run_shell", "arguments": {"command": "ls"}}\n```'
    )
    replayed = render_assistant_tool_calls(calls)
    assert replayed.startswith("```tool_call")
    # A replayed transcript must parse back to the same call.
    again, _ = parse_tool_calls(replayed)
    assert again[0].function.name == "run_shell"


class TestTheContract:
    """The half of the instructions worth installing in a declarative agent."""

    def test_it_teaches_the_loop_not_just_the_format(self):
        # Copilot's failure mode is answering after one call; the contract has to
        # say, in as many words, that a task usually takes several.
        assert "MORE THAN ONE call" in TOOL_CONTRACT
        assert "<tool_response>" in TOOL_CONTRACT
        assert "```tool_call" in TOOL_CONTRACT

    def test_the_rendered_instructions_carry_it(self):
        rendered = format_tool_instructions([SHELL_TOOL])
        assert TOOL_CONTRACT in rendered
        assert "run_shell" in rendered

    def test_it_can_be_left_out_for_an_agent_that_already_has_it(self):
        rendered = format_tool_instructions([SHELL_TOOL], include_contract=False)
        assert TOOL_CONTRACT not in rendered
        # The tool list itself still has to travel: the agent cannot know it.
        assert "run_shell" in rendered
        assert "command (string, required)" in rendered
        assert len(rendered) < len(format_tool_instructions([SHELL_TOOL]))

    def test_no_tools_means_no_instructions_either_way(self):
        assert format_tool_instructions([], include_contract=False) == ""
        assert format_tool_instructions([]) == ""
