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
