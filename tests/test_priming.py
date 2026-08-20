"""The opening exchange: what the script says, and what counts as understanding."""

from __future__ import annotations

import json

import pytest

from m365_copilot_proxy import priming
from m365_copilot_proxy.config import get_settings

SCRIPT = {
    "attempts": 2,
    "on_failure": "continue",
    "models": {
        "agent:bot": [
            {"text": "use your tools\n\n{{tools_prompt}}", "expect": "agente-ok"},
        ],
        "*": [{"text": "be brief"}],
    },
}


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("M365_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    priming.reset_cache()
    yield tmp_path
    get_settings.cache_clear()
    priming.reset_cache()


def write(data: dict) -> None:
    priming.config_path().parent.mkdir(parents=True, exist_ok=True)
    priming.config_path().write_text(json.dumps(data), encoding="utf-8")
    priming.reset_cache()


class TestLoading:
    def test_no_file_primes_nothing(self):
        assert priming.load().models == {}
        assert priming.load().steps_for("agent:bot") == []

    def test_the_file_decides_who_is_primed(self):
        write(SCRIPT)
        script = priming.load()

        assert script.steps_for("agent:bot")[0].expect == "agente-ok"
        # Anything without an entry of its own falls back to `*`.
        assert script.steps_for("claude-sonnet")[0].text == "be brief"
        assert script.attempts == 2
        assert script.fails_closed is False

    def test_a_model_with_an_empty_list_is_deliberately_not_primed(self):
        # An explicit empty list is a choice, and it must beat the `*` fallback.
        write({"models": {"agent:bot": [], "*": [{"text": "be brief"}]}})
        assert priming.load().steps_for("agent:bot") == []

    def test_a_broken_file_is_a_problem_not_an_empty_script(self):
        # Silently priming nothing is the degradation this whole round exists to
        # stop: the file was written to be used.
        priming.config_path().parent.mkdir(parents=True, exist_ok=True)
        priming.config_path().write_text("{not json", encoding="utf-8")
        priming.reset_cache()

        script = priming.load()
        assert script.models == {}
        assert not script.is_usable
        # Nothing says which models it covered, so it applies to all of them.
        assert script.problems_for("anything") != []

    def test_nonsense_values_fall_back_to_the_safe_ones(self):
        write({"attempts": -4, "on_failure": "explode", "models": {"*": ["just text"]}})
        script = priming.load()

        assert script.attempts == 3
        assert script.fails_closed is True
        # A bare string is a step with no check.
        assert script.steps_for("x")[0].text == "just text"

    def test_an_edit_is_picked_up_without_a_restart(self):
        write(SCRIPT)
        assert priming.load().attempts == 2

        write({**SCRIPT, "attempts": 5})
        assert priming.load().attempts == 5


class TestMatching:
    def test_a_substring_is_enough_however_the_model_dresses_it(self):
        step = priming.Step(text="x", expect="agente-ok")

        assert step.accepts("agente-ok")
        assert step.accepts('**AGENTE-OK**')
        assert step.accepts('Claro! "agente-ok"')
        assert not step.accepts("entendi, pode mandar")

    def test_a_regex_wins_when_both_are_given(self):
        step = priming.Step(text="x", expect="never", expect_regex=r"^\s*ok\b")
        assert step.accepts("ok, ready")
        assert not step.accepts("never mind")

    def test_an_invalid_regex_does_not_block_the_turn(self):
        assert priming.Step(text="x", expect_regex="[unclosed").accepts("anything")

    def test_a_step_with_no_check_always_passes(self):
        assert priming.Step(text="x").accepts("")
        assert priming.Step(text="x").is_checked is False


class TestPlaceholders:
    VALUES = {"tools_prompt": "[Available tools]\n- ls", "system_prompt": "be terse"}

    def test_they_are_filled_from_the_request(self):
        assert priming.render("use {{tools_prompt}} now", self.VALUES) == (
            "use [Available tools]\n- ls now"
        )
        assert priming.render("{{ system_prompt }}", self.VALUES) == "be terse"

    def test_an_unknown_one_survives_as_written(self):
        # A typo must not silently delete half the message.
        assert priming.render("a {{nope}} b", self.VALUES) == "a {{nope}} b"

    def test_a_step_that_renders_empty_is_skipped(self):
        # `{{tools_prompt}}` costs nothing when the client declared no tools.
        steps = [priming.Step(text="{{tools_prompt}}"), priming.Step(text="always")]
        rendered = priming.rendered_steps(steps, {"tools_prompt": ""})

        assert [s.text for s in rendered] == ["always"]

    def test_rendering_keeps_what_makes_a_step_checkable(self):
        steps = [priming.Step(text="{{tools_prompt}}", expect="ok", label="tools")]
        rendered = priming.rendered_steps(steps, self.VALUES)

        assert rendered[0].expect == "ok"
        assert rendered[0].describe() == "tools"


class TestProblems:
    """A step that cannot be read is refused, never quietly skipped."""

    #: The file that prompted this: `texto` where the schema says `text`. It loaded
    #: one step of two and said nothing.
    REPORTED = {
        "attempts": 1,
        "on_failure": "fail",
        "models": {
            "agent:agent-1": [
                {"label": "use the tools", "text": "use your tools", "expect": "agente-ok"},
                {"label": "system prompt", "texto": "the personality", "expect": "agente-ok2"},
            ]
        },
    }

    def test_the_reported_typo_is_named_and_guessed(self):
        write(self.REPORTED)
        problems = priming.load().problems_for("agent:agent-1")

        assert len(problems) == 1
        assert "step 2" in problems[0]
        assert "`texto`" in problems[0]
        assert "did you mean `text`?" in problems[0]

    def test_a_typo_in_expect_is_a_problem_too(self):
        # Otherwise the step runs unchecked, which looks like passing.
        write({"models": {"*": [{"text": "hi", "expects": "ok"}]}})

        assert "did you mean `expect`?" in priming.load().problems_for("x")[0]

    def test_a_step_with_no_text_lists_the_keys_it_does_have(self):
        write({"models": {"*": [{"label": "x", "expect": "ok"}]}})

        problem = priming.load().problems_for("x")[0]
        assert "no usable `text`" in problem
        assert "`label`" in problem

    def test_a_valid_script_has_no_problems(self):
        write(SCRIPT)
        script = priming.load()

        assert script.is_usable
        assert script.problems_for("agent:bot") == []
        assert len(script.steps_for("agent:bot")) == 1

    def test_a_broken_entry_does_not_block_another_model(self):
        write(
            {
                "models": {
                    "broken": [{"texto": "x"}],
                    "agent:fine": [{"text": "hi", "expect": "ok"}],
                }
            }
        )
        script = priming.load()

        assert script.problems_for("agent:fine") == []
        assert script.problems_for("broken") != []

    def test_a_broken_entry_does_not_fall_through_to_the_star_one(self):
        # Its own entry is what serves it, however little of it survived parsing.
        write({"models": {"broken": [{"texto": "x"}], "*": [{"text": "fallback"}]}})
        script = priming.load()

        assert script.problems_for("broken") != []
        assert script.steps_for("broken") == []

    def test_an_unknown_top_level_key_is_caught(self):
        write({"atempts": 5, "models": {"*": [{"text": "hi"}]}})

        assert "did you mean `attempts`?" in priming.load().problems_for("x")[0]

    def test_a_step_that_is_not_an_object_says_what_it_is(self):
        write({"models": {"*": [42]}})

        assert "not an object" in priming.load().problems_for("x")[0]

    def test_the_message_names_the_file_and_the_way_out(self):
        write(self.REPORTED)
        report = priming.describe_problems(priming.load().problems_for("agent:agent-1"))

        assert str(priming.config_path()) in report
        assert "M365_PRIMING=0" in report
