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

    def test_a_broken_file_is_ignored_not_fatal(self):
        priming.config_path().parent.mkdir(parents=True, exist_ok=True)
        priming.config_path().write_text("{not json", encoding="utf-8")
        priming.reset_cache()

        assert priming.load().models == {}

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
