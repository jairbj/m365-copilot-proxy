"""Generating pi's provider config from the capture."""

from __future__ import annotations

import json

import pytest

from m365_copilot_proxy import pi_config
from m365_copilot_proxy.bizchat import profile as tenant_profile
from m365_copilot_proxy.config import get_settings

PROFILE = {
    "tones": {"claude-sonnet": "Claude_Sonnet", "gpt-5.6-chat": "Gpt_5_6_Chat"},
    "agents": {
        "sales-bot": {
            "thread_level_gpt_id": {"id": "gpt-guid"},
            "surface": {"query": {"agent": "Agent"}},
        }
    },
}


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("M365_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    tenant_profile.reset_cache()
    yield tmp_path
    get_settings.cache_clear()
    tenant_profile.reset_cache()


def write_profile(data: dict) -> None:
    tenant_profile.profile_path().parent.mkdir(parents=True, exist_ok=True)
    tenant_profile.profile_path().write_text(json.dumps(data), encoding="utf-8")
    tenant_profile.reset_cache()


class TestWhichModels:
    def test_captured_tones_come_in_both_surfaces(self):
        write_profile(PROFILE)

        assert pi_config.model_ids() == [
            "claude-sonnet",
            "claude-sonnet-work",
            "gpt-5.6-chat",
            "gpt-5.6-chat-work",
            "agent:sales-bot",
        ]

    def test_an_agent_is_offered_once(self):
        # No Work IQ toggle in the agent UI, so no twin to offer.
        write_profile(PROFILE)
        assert "agent:sales-bot-work" not in pi_config.model_ids()

    def test_the_built_in_aliases_are_left_out(self):
        # They describe a different tenant at a different time; the picker should
        # show what this capture actually saw.
        write_profile(PROFILE)
        ids = pi_config.model_ids()

        assert "think-deeper" not in ids
        assert "gpt-5.2" not in ids

    def test_nothing_captured_is_an_error_the_user_can_act_on(self):
        with pytest.raises(pi_config.NothingCaptured, match="capture"):
            pi_config.provider()

    def test_a_profile_with_only_an_agent_still_produces_a_config(self):
        write_profile({"agents": PROFILE["agents"]})
        assert pi_config.model_ids() == ["agent:sales-bot"]


class TestTheBlock:
    def test_it_matches_the_shape_the_shipped_example_uses(self):
        write_profile(PROFILE)
        block = pi_config.provider()

        assert set(block) == {"baseUrl", "api", "apiKey", "compat", "models"}
        assert block["api"] == "openai-completions"
        assert block["compat"]["supportsDeveloperRole"] is False
        # pi hides models with no auth configured; the proxy ignores the value.
        assert block["apiKey"] == "unused"

    def test_the_base_url_follows_the_configured_bind_address(self, monkeypatch):
        monkeypatch.setenv("M365_PORT", "9999")
        get_settings.cache_clear()
        write_profile(PROFILE)

        assert pi_config.provider()["baseUrl"] == "http://127.0.0.1:9999/v1"

    def test_every_id_it_writes_is_one_the_proxy_serves(self):
        from m365_copilot_proxy.bizchat import protocol

        write_profile(PROFILE)
        for model in pi_config.provider()["models"]:
            base, _ = protocol.parse_model(model["id"])
            assert base in protocol.model_tones() or protocol.agent_for_model(base)


class TestMerging:
    def test_other_providers_and_settings_survive(self):
        existing = {
            "defaultModel": "m365/claude-sonnet",
            "providers": {"someone-else": {"baseUrl": "http://elsewhere"}},
        }
        merged = pi_config.merge_into(existing, {"models": []})

        assert merged["defaultModel"] == "m365/claude-sonnet"
        assert merged["providers"]["someone-else"] == {"baseUrl": "http://elsewhere"}
        assert merged["providers"]["m365"] == {"models": []}

    def test_our_own_block_is_replaced_not_appended_to(self):
        existing = {"providers": {"m365": {"models": [{"id": "stale"}]}}}
        merged = pi_config.merge_into(existing, {"models": [{"id": "fresh"}]})

        assert merged["providers"]["m365"]["models"] == [{"id": "fresh"}]

    def test_an_empty_or_unusable_document_still_yields_a_config(self):
        assert pi_config.merge_into({}, {"models": []})["providers"]["m365"] == {"models": []}
        assert pi_config.merge_into("not a dict", {"models": []})["providers"]["m365"] == {
            "models": []
        }
