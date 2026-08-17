"""Watching the real client: what the collector learns from URLs and frames."""

from __future__ import annotations

import json

from m365_copilot_proxy.bizchat import frames
from m365_copilot_proxy.capture import ProfileCollector

CHATHUB_URL = (
    "wss://substrate.office.com/m365Copilot/Chathub/oid@tid"
    "?chatsessionid=abc&access_token=SECRET-TOKEN&variants=feature.a,feature.b"
    "&source=%22officeweb%22&product=Office&agentHost=Bizchat.FullScreen"
    "&licenseType=Premium&agent=work&scenario=officeweb&isEdu=false"
)


def chat_frame(tone: str, **extra) -> str:
    return frames.encode(
        {
            "type": 4,
            "target": "chat",
            "invocationId": "0",
            "arguments": [{"message": {"text": "hi"}, "tone": tone, **extra}],
        }
    )


class TestNoteUrl:
    def test_the_tenant_surface_is_recorded(self):
        collector = ProfileCollector()
        assert collector.note_url(CHATHUB_URL) is True
        assert collector.query["agent"] == "work"
        assert collector.query["scenario"] == "officeweb"
        assert collector.query["licenseType"] == "Premium"
        assert collector.query["variants"] == "feature.a,feature.b"

    def test_the_access_token_is_never_recorded(self):
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL)
        assert "SECRET-TOKEN" not in json.dumps(collector.build().to_json())
        assert "access_token" not in collector.query

    def test_unrelated_sockets_are_ignored(self):
        collector = ProfileCollector()
        assert collector.note_url("wss://example.invalid/telemetry?agent=nonsense") is False
        assert collector.query == {}


class TestNoteFramePayload:
    def test_a_tone_is_discovered_and_given_a_model_id(self):
        collector = ProfileCollector()
        discovered = collector.note_frame_payload(1, chat_frame("Gpt_5_6_Quick"))
        assert discovered == ["Gpt_5_6_Quick"]
        assert collector.tones == {"gpt-5.6-quick": "Gpt_5_6_Quick"}

    def test_the_same_tone_is_only_reported_once(self):
        collector = ProfileCollector()
        collector.note_frame_payload(1, chat_frame("Claude_Opus"))
        assert collector.note_frame_payload(1, chat_frame("Claude_Opus")) == []
        assert len(collector.tones) == 1

    def test_frames_arriving_glued_together_are_both_read(self):
        collector = ProfileCollector()
        payload = chat_frame("Claude_Sonnet") + frames.encode(frames.build_metrics())
        assert collector.note_frame_payload(1, payload) == ["Claude_Sonnet"]

    def test_a_frame_split_across_payloads_is_reassembled(self):
        collector = ProfileCollector()
        whole = chat_frame("Gpt_Reasoning")
        half = len(whole) // 2
        assert collector.note_frame_payload(1, whole[:half]) == []
        assert collector.note_frame_payload(1, whole[half:]) == ["Gpt_Reasoning"]

    def test_buffers_do_not_bleed_between_sockets(self):
        collector = ProfileCollector()
        whole = chat_frame("Claude_Opus")
        collector.note_frame_payload(1, whole[:10])
        assert collector.note_frame_payload(2, whole) == ["Claude_Opus"]

    def test_option_sets_and_allowed_types_are_captured(self):
        collector = ProfileCollector()
        collector.note_frame_payload(
            1,
            chat_frame(
                "magic",
                optionsSets=["tenant_set_a", "tenant_set_b"],
                allowedMessageTypes=["Chat", "Progress"],
            ),
        )
        assert collector.option_sets == ["tenant_set_a", "tenant_set_b"]
        assert collector.allowed_message_types == ["Chat", "Progress"]

    def test_non_chat_frames_are_ignored(self):
        collector = ProfileCollector()
        payload = frames.encode(frames.build_metrics()) + frames.encode({"type": 6})
        assert collector.note_frame_payload(1, payload) == []
        assert collector.tones == {}

    def test_malformed_payloads_do_not_raise(self):
        collector = ProfileCollector()
        assert collector.note_frame_payload(1, "not json\x1e") == []


def test_build_produces_a_saveable_profile():
    collector = ProfileCollector()
    collector.note_url(CHATHUB_URL)
    collector.note_frame_payload(1, chat_frame("Claude_Sonnet"))

    profile = collector.build()
    assert profile.is_empty is False
    assert profile.tones == {"claude-sonnet": "Claude_Sonnet"}
    assert profile.query["agent"] == "work"
