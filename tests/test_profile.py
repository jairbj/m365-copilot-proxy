"""Tenant profile: id derivation, loading, and overriding the built-ins."""

from __future__ import annotations

import json

import pytest

from m365_copilot_proxy.bizchat import profile as tenant_profile
from m365_copilot_proxy.bizchat import protocol
from m365_copilot_proxy.bizchat.profile import TenantProfile, slug_for_tone
from m365_copilot_proxy.config import get_settings


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


def test_captured_surface_overrides_the_built_in_query():
    # The work/enterprise surface differs from the individual one exactly here.
    write_profile({"query": {"agent": "work", "scenario": "officeweb", "licenseType": "Premium"}})

    query = protocol.query_defaults()
    assert query["agent"] == "work"
    assert query["scenario"] == "officeweb"
    assert query["licenseType"] == "Premium"
    # Untouched fields keep their defaults.
    assert query["product"] == protocol.QUERY_DEFAULTS["product"]


def test_captured_variants_replace_the_built_in_list():
    write_profile({"query": {"variants": "feature.one,feature.two"}})
    assert protocol.variants() == "feature.one,feature.two"


def test_captured_tones_add_and_override_models():
    write_profile(
        {"tones": {"gpt-5.7-quick": "Gpt_5_7_Quick", "claude-sonnet": "Claude_Sonnet_V9"}}
    )

    assert protocol.tone_for_model("gpt-5.7-quick") == "Gpt_5_7_Quick"
    assert protocol.tone_for_model("claude-sonnet") == "Claude_Sonnet_V9"
    assert "gpt-5.7-quick" in protocol.available_models()
    # Built-ins the capture did not mention survive.
    assert protocol.tone_for_model("think-deeper") == "Gpt_Reasoning"


def test_captured_option_sets_replace_the_built_ins_and_still_add_images():
    write_profile({"option_sets": ["tenant_specific_set"]})

    assert protocol.option_sets() == ["tenant_specific_set"]
    with_images = protocol.option_sets(generate_images=True)
    assert with_images[0] == "tenant_specific_set"
    assert "cwc_flux_image" in with_images


def test_captured_allowed_types_still_declare_the_image_type():
    write_profile({"allowed_message_types": ["Chat", "Progress"]})

    assert protocol.allowed_message_types() == ["Chat", "Progress"]
    assert protocol.IMAGE_MESSAGE_TYPE in protocol.allowed_message_types(generate_images=True)


def test_save_then_load_round_trips():
    saved = TenantProfile(query={"agent": "work"}, tones={"x": "X_Tone"})
    path = tenant_profile.save(saved)
    assert path.exists()

    loaded = tenant_profile.load()
    assert loaded.query == {"agent": "work"}
    assert loaded.tones == {"x": "X_Tone"}
    assert loaded.captured_at  # stamped on save


def test_a_new_capture_is_picked_up_without_a_restart():
    write_profile({"tones": {"a": "A"}})
    assert protocol.tone_for_model("a") == "A"

    tenant_profile.save(TenantProfile(tones={"b": "B"}))
    assert protocol.tone_for_model("b") == "B"
