"""Tenant profile: surfaces, id derivation, and overriding the built-ins."""

from __future__ import annotations

import json

import pytest

from m365_copilot_proxy.bizchat import profile as tenant_profile
from m365_copilot_proxy.bizchat import protocol
from m365_copilot_proxy.bizchat.profile import WEB, WORK, Surface, TenantProfile, slug_for_tone
from m365_copilot_proxy.config import get_settings

#: What the two surfaces actually look like, shortened from a real capture: the
#: toggle swaps the agent, the scenario, the variants and the whole optionsSets
#: family at once.
WORK_SURFACE = {
    "query": {"agent": "work", "scenario": "officeweb", "variants": "feature.work"},
    "option_sets": ["enterprise_flux_work", "at_mention_plugins_enable"],
    "allowed_message_types": ["Chat", "ReferencesListComplete"],
}
WEB_SURFACE = {
    "query": {"agent": "web", "scenario": "OfficeWebPaidCopilot", "variants": "feature.web"},
    "option_sets": ["cwc_flux_v3", "cwc_code_interpreter"],
    "allowed_message_types": ["Chat", "Progress"],
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
    (get_settings().config_dir).mkdir(parents=True, exist_ok=True)
    tenant_profile.profile_path().write_text(json.dumps(data), encoding="utf-8")
    tenant_profile.reset_cache()


def write_surfaces(**surfaces: dict) -> None:
    write_profile({"surfaces": surfaces})


class TestSlugForTone:
    @pytest.mark.parametrize(
        ("tone", "expected"),
        [
            ("Claude_Sonnet", "claude-sonnet"),
            ("Claude_Sonnet_Reasoning", "claude-sonnet-reasoning"),
            ("Gpt_Quick", "gpt-quick"),
            # Consecutive numbers are a version, not separate words.
            ("Gpt_5_6_Quick", "gpt-5.6-quick"),
            ("Gpt_5_6_Reasoning", "gpt-5.6-reasoning"),
            ("magic", "magic"),
        ],
    )
    def test_derives_a_usable_model_id(self, tone, expected):
        assert slug_for_tone(tone) == expected


def test_no_profile_means_the_built_in_defaults():
    assert tenant_profile.load().is_empty
    assert protocol.query_defaults() == protocol.QUERY_DEFAULTS
    assert protocol.variants() == ",".join(protocol.VARIANTS)
    assert protocol.tone_for_model("claude-sonnet") == "Claude_Sonnet"


def test_a_corrupt_profile_is_ignored_rather_than_fatal():
    (get_settings().config_dir).mkdir(parents=True, exist_ok=True)
    tenant_profile.profile_path().write_text("{not json", encoding="utf-8")
    tenant_profile.reset_cache()

    assert tenant_profile.load().is_empty
    assert protocol.query_defaults() == protocol.QUERY_DEFAULTS


class TestSurfacesSwapWholesale:
    """The whole point: a turn never mixes fields from the two surfaces."""

    def test_work_iq_on_serves_the_work_surface(self):
        write_surfaces(work=WORK_SURFACE, web=WEB_SURFACE)

        query = protocol.query_defaults(True)
        assert query["agent"] == "work"
        assert query["scenario"] == "officeweb"
        assert protocol.variants(True) == "feature.work"
        assert protocol.option_sets(work_iq=True) == WORK_SURFACE["option_sets"]
        assert protocol.allowed_message_types(work_iq=True) == ["Chat", "ReferencesListComplete"]

    def test_work_iq_off_serves_the_web_surface(self):
        write_surfaces(work=WORK_SURFACE, web=WEB_SURFACE)

        query = protocol.query_defaults(False)
        assert query["agent"] == "web"
        assert query["scenario"] == "OfficeWebPaidCopilot"
        assert protocol.variants(False) == "feature.web"
        assert protocol.option_sets(work_iq=False) == WEB_SURFACE["option_sets"]
        assert protocol.allowed_message_types(work_iq=False) == ["Chat", "Progress"]

    def test_no_field_ever_crosses_between_surfaces(self):
        # `agent=web` with the enterprise scenario and optionsSets is a combination
        # no real client sends, and the server would accept it in silence.
        write_surfaces(work=WORK_SURFACE, web=WEB_SURFACE)

        for work_iq, surface in ((True, WORK_SURFACE), (False, WEB_SURFACE)):
            query = protocol.query_defaults(work_iq)
            assert query["scenario"] == surface["query"]["scenario"]
            assert protocol.variants(work_iq) == surface["query"]["variants"]
            assert protocol.option_sets(work_iq=work_iq) == surface["option_sets"]


def test_only_one_surface_captured_is_used_for_both(caplog):
    write_surfaces(work=WORK_SURFACE)

    with caplog.at_level("WARNING"):
        query = protocol.query_defaults(False)
    # Half a profile still beats the inherited defaults, but it must not be silent.
    assert query["agent"] == "work"
    assert "capture" in caplog.text


def test_a_legacy_flat_profile_migrates_into_the_slot_its_agent_names():
    write_profile({**WORK_SURFACE, "tones": {"x": "X_Tone"}})

    profile = tenant_profile.load()
    assert profile.has_surface(WORK)
    assert not profile.has_surface(WEB)
    assert protocol.query_defaults(True)["scenario"] == "officeweb"
    assert protocol.tone_for_model("x") == "X_Tone"


def test_captured_tones_are_shared_across_surfaces():
    # The second capture proved the tone list is identical either way, so it lives
    # once at the top of the file rather than duplicated per surface.
    write_profile({"surfaces": {"work": WORK_SURFACE, "web": WEB_SURFACE}, "tones": {"a": "A"}})

    assert protocol.tone_for_model("a") == "A"
    assert "a" in protocol.available_models()


def test_captured_tones_add_and_override_models():
    write_profile(
        {"tones": {"gpt-5.7-quick": "Gpt_5_7_Quick", "claude-sonnet": "Claude_Sonnet_V9"}}
    )

    assert protocol.tone_for_model("gpt-5.7-quick") == "Gpt_5_7_Quick"
    assert protocol.tone_for_model("claude-sonnet") == "Claude_Sonnet_V9"
    assert "gpt-5.7-quick" in protocol.available_models()
    # Built-ins the capture did not mention survive.
    assert protocol.tone_for_model("think-deeper") == "Gpt_Reasoning"


def test_image_generation_still_layers_on_top_of_a_captured_surface():
    write_surfaces(work=WORK_SURFACE)

    with_images = protocol.option_sets(work_iq=True, generate_images=True)
    assert with_images[: len(WORK_SURFACE["option_sets"])] == WORK_SURFACE["option_sets"]
    assert "cwc_flux_image" in with_images
    assert protocol.IMAGE_MESSAGE_TYPE in protocol.allowed_message_types(
        work_iq=True, generate_images=True
    )


def test_captured_plugins_replace_the_built_in_default():
    write_surfaces(web={**WEB_SURFACE, "plugins": [{"Id": "Tenant", "Source": "BuiltIn"}]})

    assert protocol.plugins(False) == [{"Id": "Tenant", "Source": "BuiltIn"}]


def test_a_surface_that_captured_no_plugins_sends_none():
    # "This surface sends no plugins" is an observation, not an absence — sending
    # Bing anyway would be inventing a capability the real client does not use.
    write_surfaces(web={**WEB_SURFACE, "plugins": []})

    assert protocol.plugins(False) == []


def test_a_surface_captured_before_plugins_existed_keeps_the_built_in():
    write_surfaces(web=WEB_SURFACE)  # no plugins key at all

    assert protocol.plugins(False) == [protocol.BING_PLUGIN]


class TestTheDefaultSurface:
    """`work_iq=None` means "whatever is configured", which is off by default."""

    def test_unspecified_means_web(self):
        write_surfaces(work=WORK_SURFACE, web=WEB_SURFACE)
        assert protocol.query_defaults()["agent"] == "web"
        assert protocol.option_sets() == WEB_SURFACE["option_sets"]

    def test_the_setting_can_flip_it(self, monkeypatch):
        write_surfaces(work=WORK_SURFACE, web=WEB_SURFACE)
        monkeypatch.setenv("M365_WORK_IQ", "1")
        get_settings.cache_clear()

        assert protocol.query_defaults()["agent"] == "work"
        assert protocol.option_sets() == WORK_SURFACE["option_sets"]

    def test_an_explicit_choice_still_wins_over_the_setting(self, monkeypatch):
        write_surfaces(work=WORK_SURFACE, web=WEB_SURFACE)
        monkeypatch.setenv("M365_WORK_IQ", "1")
        get_settings.cache_clear()

        assert protocol.query_defaults(False)["agent"] == "web"


def test_without_a_profile_the_agent_is_the_one_thing_we_can_still_set():
    assert protocol.query_defaults(True)["agent"] == protocol.AGENT_WORK
    assert protocol.query_defaults(False)["agent"] == protocol.AGENT_WEB
    assert protocol.query_defaults()["agent"] == protocol.AGENT_WEB


def test_save_then_load_round_trips():
    saved = TenantProfile(
        surfaces={WORK: Surface(query={"agent": "work"}, option_sets=["a"])},
        tones={"x": "X_Tone"},
    )
    path = tenant_profile.save(saved)
    assert path.exists()

    loaded = tenant_profile.load()
    assert loaded.surfaces[WORK].query == {"agent": "work"}
    assert loaded.surfaces[WORK].option_sets == ["a"]
    assert loaded.tones == {"x": "X_Tone"}
    assert loaded.captured_at  # stamped on save


def test_a_new_capture_is_picked_up_without_a_restart():
    write_profile({"tones": {"a": "A"}})
    assert protocol.tone_for_model("a") == "A"

    tenant_profile.save(TenantProfile(tones={"b": "B"}))
    assert protocol.tone_for_model("b") == "B"
