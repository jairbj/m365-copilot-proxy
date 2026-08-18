"""Model id -> tone resolution."""

from __future__ import annotations

from m365_copilot_proxy.bizchat import protocol


def test_known_ids_map_to_their_tone():
    assert protocol.tone_for_model("m365-copilot") == "magic"
    assert protocol.tone_for_model("claude-sonnet") == "Claude_Sonnet"
    assert protocol.tone_for_model("gpt-5.6-think-deeper") == "Gpt_5_6_Reasoning"


def test_unmapped_claude_ids_stay_on_a_claude_tone():
    # A client sending `claude-opus-4-8[1m]` must not silently get GPT.
    assert protocol.tone_for_model("claude-opus-4-8[1m]") == "Claude_Sonnet"
    assert protocol.tone_for_model("Claude-Whatever") == "Claude_Sonnet"


def test_unknown_and_missing_ids_fall_back_to_the_default():
    assert protocol.tone_for_model("gpt-4o") == "magic"
    assert protocol.tone_for_model(None) == "magic"
    assert protocol.tone_for_model("") == "magic"


def test_image_model_is_advertised():
    assert protocol.IMAGE_MODEL in protocol.available_models()


class TestParseModel:
    """The `-work` suffix selects the surface without disturbing the tone lookup."""

    def test_no_suffix_leaves_the_choice_to_the_caller(self):
        assert protocol.parse_model("claude-sonnet") == ("claude-sonnet", None)

    def test_the_work_suffix_turns_grounding_on(self):
        assert protocol.parse_model("claude-sonnet-work") == ("claude-sonnet", True)

    def test_the_web_suffix_turns_grounding_off(self):
        assert protocol.parse_model("claude-sonnet-web") == ("claude-sonnet", False)

    def test_a_real_model_id_is_never_split(self, monkeypatch):
        # If Microsoft ever ships a tone whose slug ends in `-work`, the model wins
        # over the suffix convention.
        monkeypatch.setitem(protocol.MODEL_TONES, "agent-work", "Agent_Work")
        assert protocol.parse_model("agent-work") == ("agent-work", None)

    def test_an_unknown_id_still_parses(self):
        assert protocol.parse_model("gpt-4o-work") == ("gpt-4o", True)

    def test_an_empty_id_is_harmless(self):
        assert protocol.parse_model(None) == ("", None)
        assert protocol.parse_model("") == ("", None)

    def test_the_suffix_does_not_break_tone_resolution(self):
        base, work_iq = protocol.parse_model("gpt-5.5-work")
        # Without stripping, `gpt-5.5-work` would fall through to the default tone.
        assert protocol.tone_for_model(base) == protocol.MODEL_TONES["gpt-5.5"]
        assert work_iq is True


def test_available_models_offers_a_work_variant_of_each():
    models = protocol.available_models()
    assert "claude-sonnet" in models
    assert "claude-sonnet-work" in models
    # `-web` is accepted on input but not advertised: it is the default already.
    assert "claude-sonnet-web" not in models
