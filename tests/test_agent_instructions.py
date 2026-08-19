"""The instructions to paste into a declarative agent: composing and recording."""

from __future__ import annotations

import pytest

from m365_copilot_proxy import agent_instructions
from m365_copilot_proxy.config import get_settings
from m365_copilot_proxy.openai_api.tools import TOOL_CONTRACT


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("M365_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


class TestCompose:
    def test_the_contract_comes_first_then_the_prompt(self):
        document = agent_instructions.compose("be terse")

        assert document.text.startswith(TOOL_CONTRACT)
        assert document.text.endswith("be terse")
        assert [title for title, _ in document.breakdown()] == ["Tool calling", "System prompt"]

    def test_either_half_can_stand_alone(self):
        assert agent_instructions.compose("be terse", contract=False).text == "be terse"
        assert agent_instructions.compose("be terse", prompt=False).text == TOOL_CONTRACT
        # The contract needs nothing recorded to exist.
        assert agent_instructions.compose().text == TOOL_CONTRACT

    def test_a_document_that_fits_says_so(self):
        document = agent_instructions.compose("be terse")

        assert document.chars < agent_instructions.INSTRUCTIONS_LIMIT
        assert document.fits
        assert document.over_by == 0

    def test_an_oversized_document_is_reported_not_trimmed(self):
        # Which paragraph to drop is the user's call: cutting it here would leave an
        # agent that silently disagrees with the prompt the client keeps sending.
        document = agent_instructions.compose("x" * 9000)

        assert not document.fits
        assert document.over_by == document.chars - agent_instructions.INSTRUCTIONS_LIMIT
        assert "x" * 9000 in document.text

    def test_the_json_shape_carries_the_measurements(self):
        payload = agent_instructions.compose("be terse", key="abc", model="claude-sonnet").to_json()

        assert payload["limit"] == agent_instructions.INSTRUCTIONS_LIMIT
        assert payload["chars"] == len(payload["text"])
        assert payload["source"]["key"] == "abc"
        assert payload["source"]["model"] == "claude-sonnet"


class TestRecording:
    def test_a_prompt_is_recorded_and_read_back(self):
        agent_instructions.record("key1", "be terse", model="claude-sonnet", label="hello")

        entry = agent_instructions.load("key1")
        assert entry is not None
        assert entry.system_text == "be terse"
        assert entry.model == "claude-sonnet"
        assert entry.label == "hello"
        assert entry.compose().text.endswith("be terse")

    def test_the_latest_one_wins(self):
        agent_instructions.record("old", "first")
        agent_instructions.record("new", "second")

        latest = agent_instructions.latest()
        assert latest is not None and latest.system_text == "second"
        assert {e.key for e in agent_instructions.list_records()} == {"old", "new"}

    def test_an_unchanged_prompt_is_not_rewritten(self):
        path = agent_instructions.record("key1", "be terse")
        assert path is not None
        stamp = path.stat().st_mtime_ns

        agent_instructions.record("key1", "be terse")
        assert path.stat().st_mtime_ns == stamp

    def test_an_empty_prompt_records_nothing(self):
        assert agent_instructions.record("key1", "   ") is None
        assert agent_instructions.list_records() == []

    def test_recording_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("M365_RECORD_SYSTEM_PROMPTS", "0")
        get_settings.cache_clear()

        assert agent_instructions.record("key1", "be terse") is None
        assert agent_instructions.latest() is None

    def test_only_the_recent_ones_are_kept(self):
        for index in range(agent_instructions.KEEP_RECORDS + 5):
            agent_instructions.record(f"key{index}", f"prompt {index}")

        assert len(agent_instructions.list_records()) == agent_instructions.KEEP_RECORDS

    def test_a_key_cannot_escape_the_directory(self):
        agent_instructions.record("../../etc/passwd", "be terse")

        written = list(agent_instructions.records_dir().glob("*.json"))
        assert len(written) == 1
        assert written[0].parent == agent_instructions.records_dir()

    def test_an_unreadable_record_is_ignored_not_raised(self):
        agent_instructions.record("key1", "be terse")
        next(iter(agent_instructions.records_dir().glob("*.json"))).write_text("{not json")

        assert agent_instructions.list_records() == []
        assert agent_instructions.latest() is None


class TestTheToolList:
    """The tool list moved into the agent, so the export has to carry it."""

    TOOLS = "[Available tools]\nTools:\n- run_shell: Run a shell command"

    def test_the_document_reads_in_the_order_a_model_needs(self):
        document = agent_instructions.compose("be terse", tool_text=self.TOOLS)

        # How to call, what to call, then who to be.
        assert [title for title, _ in document.breakdown()] == [
            "Tool calling",
            "Available tools",
            "System prompt",
        ]
        assert document.text.index("run_shell") < document.text.index("be terse")

    def test_each_part_can_stand_alone(self):
        only_tools = agent_instructions.compose(
            "be terse", tool_text=self.TOOLS, contract=False, prompt=False
        )
        assert only_tools.text == self.TOOLS

    def test_a_client_that_declares_no_tools_contributes_no_section(self):
        document = agent_instructions.compose("be terse", tool_text="")
        assert [title for title, _ in document.breakdown()] == ["Tool calling", "System prompt"]

    def test_the_tool_list_is_recorded_and_read_back(self):
        agent_instructions.record("key1", "be terse", tool_text=self.TOOLS)

        entry = agent_instructions.load("key1")
        assert entry is not None
        assert entry.tool_text == self.TOOLS
        assert "run_shell" in entry.compose().text

    def test_tools_alone_are_worth_recording(self):
        # An agent turn sends no tool list, so this may be all there is to paste.
        assert agent_instructions.record("key1", "", tool_text=self.TOOLS) is not None
        latest = agent_instructions.latest()
        assert latest is not None and latest.tool_text == self.TOOLS

    def test_a_changed_tool_list_is_written_even_when_the_prompt_is_the_same(self):
        path = agent_instructions.record("key1", "be terse", tool_text=self.TOOLS)
        assert path is not None

        agent_instructions.record("key1", "be terse", tool_text=self.TOOLS + "\n- ls: list")
        reloaded = agent_instructions.load("key1")
        assert reloaded is not None and "ls: list" in reloaded.tool_text

    def test_a_record_from_before_this_still_loads(self):
        entry = agent_instructions.Record.from_json(
            {"key": "old", "system_text": "be terse", "model": "claude-sonnet"}
        )
        assert entry is not None
        assert entry.tool_text == ""
        assert entry.compose().text.endswith("be terse")

    def test_nothing_at_all_records_nothing(self):
        assert agent_instructions.record("key1", "  ", tool_text="  ") is None
