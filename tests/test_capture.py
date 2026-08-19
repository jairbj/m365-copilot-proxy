"""Watching the real client: what the collector learns from URLs and frames."""

from __future__ import annotations

import json

from m365_copilot_proxy.bizchat import frames
from m365_copilot_proxy.capture import ApiRecorder, ProfileCollector, redact_url


def names(observations) -> list[str]:
    """The names of what a payload taught us, whatever kind each one is."""
    return [observation.name for observation in observations]

CHATHUB_URL = (
    "wss://substrate.office.com/m365Copilot/Chathub/oid@tid"
    "?chatsessionid=abc&access_token=SECRET-TOKEN&variants=feature.a,feature.b"
    "&source=%22officeweb%22&product=Office&agentHost=Bizchat.FullScreen"
    "&licenseType=Premium&agent=work&scenario=officeweb&isEdu=false"
)


def chat_frame(tone: str | None, **extra) -> str:
    """One outbound chat invocation. `tone=None` omits the field, as an agent does."""
    argument: dict = {"message": {"text": "hi"}, **extra}
    if tone is not None:
        argument["tone"] = tone
    return frames.encode(
        {"type": 4, "target": "chat", "invocationId": "0", "arguments": [argument]}
    )


class TestNoteUrl:
    def test_the_url_names_the_surface_it_belongs_to(self):
        # No flag to remember: the `agent` field says which side of the Work IQ
        # toggle this capture is recording.
        collector = ProfileCollector()
        assert collector.note_url(CHATHUB_URL, socket_id=1) == "work"

        collector.note_frame_payload(1, chat_frame("magic"))
        surface = collector.surfaces["work"]
        assert surface.query["scenario"] == "officeweb"
        assert surface.query["licenseType"] == "Premium"
        assert surface.query["variants"] == "feature.a,feature.b"

    def test_a_connection_alone_files_nothing(self):
        # Until a turn is sent there is no telling whether this thread belongs to a
        # surface or to a declarative agent, and an agent must not overwrite one.
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL, socket_id=1)
        assert collector.surfaces == {}
        assert collector.agents == {}

    def test_two_runs_fill_two_surfaces(self):
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL, socket_id=1)
        collector.note_url(CHATHUB_URL.replace("agent=work", "agent=web"), socket_id=2)
        collector.note_frame_payload(1, chat_frame("magic"))
        collector.note_frame_payload(2, chat_frame("magic"))

        assert set(collector.surfaces) == {"work", "web"}
        assert collector.surfaces["web"].query["agent"] == "web"

    def test_the_access_token_is_never_recorded(self):
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL, socket_id=1)
        collector.note_frame_payload(1, chat_frame("magic"))
        assert "SECRET-TOKEN" not in json.dumps(collector.build().to_json())
        assert "access_token" not in collector.surfaces["work"].query

    def test_the_token_never_reaches_an_agent_either(self):
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL, socket_id=1)
        collector.note_frame_payload(1, chat_frame("", threadLevelGptId={"gptId": "abc"}))
        assert "SECRET-TOKEN" not in json.dumps(collector.build().to_json())
        assert "access_token" not in collector.agents["agent-1"].surface.query

    def test_unrelated_sockets_are_ignored(self):
        collector = ProfileCollector()
        assert collector.note_url("wss://example.invalid/telemetry?agent=nonsense") is None
        assert collector.surfaces == {}


class TestNoteFramePayload:
    def test_a_tone_is_discovered_and_given_a_model_id(self):
        collector = ProfileCollector()
        discovered = collector.note_frame_payload(1, chat_frame("Gpt_5_6_Quick"))
        assert "Gpt_5_6_Quick" in names(discovered)
        assert collector.tones == {"gpt-5.6-quick": "Gpt_5_6_Quick"}

    def test_the_same_tone_is_only_reported_once(self):
        collector = ProfileCollector()
        collector.note_frame_payload(1, chat_frame("Claude_Opus"))
        assert names(collector.note_frame_payload(1, chat_frame("Claude_Opus"))) == []
        assert len(collector.tones) == 1

    def test_frames_arriving_glued_together_are_both_read(self):
        collector = ProfileCollector()
        payload = chat_frame("Claude_Sonnet") + frames.encode(frames.build_metrics())
        assert "Claude_Sonnet" in names(collector.note_frame_payload(1, payload))

    def test_a_frame_split_across_payloads_is_reassembled(self):
        collector = ProfileCollector()
        whole = chat_frame("Gpt_Reasoning")
        half = len(whole) // 2
        assert names(collector.note_frame_payload(1, whole[:half])) == []
        assert "Gpt_Reasoning" in names(collector.note_frame_payload(1, whole[half:]))

    def test_buffers_do_not_bleed_between_sockets(self):
        collector = ProfileCollector()
        whole = chat_frame("Claude_Opus")
        collector.note_frame_payload(1, whole[:10])
        assert "Claude_Opus" in names(collector.note_frame_payload(2, whole))

    def test_option_sets_plugins_and_types_land_on_the_socket_surface(self):
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL, socket_id=1)
        collector.note_frame_payload(
            1,
            chat_frame(
                "magic",
                optionsSets=["tenant_set_a", "tenant_set_b"],
                allowedMessageTypes=["Chat", "Progress"],
                plugins=[{"Id": "BingWebSearch", "Source": "BuiltIn"}],
            ),
        )
        surface = collector.surfaces["work"]
        assert surface.option_sets == ["tenant_set_a", "tenant_set_b"]
        assert surface.allowed_message_types == ["Chat", "Progress"]
        assert surface.plugins == [{"Id": "BingWebSearch", "Source": "BuiltIn"}]

    def test_frames_from_different_surfaces_do_not_mix(self):
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL, socket_id=1)
        collector.note_url(CHATHUB_URL.replace("agent=work", "agent=web"), socket_id=2)
        collector.note_frame_payload(1, chat_frame("magic", optionsSets=["enterprise_flux_work"]))
        collector.note_frame_payload(2, chat_frame("magic", optionsSets=["cwc_flux_v3"]))

        assert collector.surfaces["work"].option_sets == ["enterprise_flux_work"]
        assert collector.surfaces["web"].option_sets == ["cwc_flux_v3"]

    def test_non_chat_frames_are_ignored(self):
        collector = ProfileCollector()
        payload = frames.encode(frames.build_metrics()) + frames.encode({"type": 6})
        assert collector.note_frame_payload(1, payload) == []
        assert collector.tones == {}

    def test_malformed_payloads_do_not_raise(self):
        collector = ProfileCollector()
        assert collector.note_frame_payload(1, "not json\x1e") == []


class TestDeclarativeAgents:
    """A turn sent inside a custom agent, which the proxy can re-enter later."""

    def test_an_agent_turn_is_filed_as_an_agent(self):
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL, socket_id=1)
        seen = collector.note_frame_payload(
            1,
            chat_frame(
                "",
                threadLevelGptId={"gptId": "agent-guid", "scope": "tenant"},
                extraExtensionParameters={"foo": "bar"},
                optionsSets=["agent_set"],
                plugins=[{"Id": "Custom", "Source": "Gpt"}],
            ),
        )
        assert [(o.kind, o.name) for o in seen] == [("agent", "agent-1")]

        agent = collector.agents["agent-1"]
        assert agent.thread_level_gpt_id == {"gptId": "agent-guid", "scope": "tenant"}
        assert agent.extra_extension_parameters == {"foo": "bar"}
        assert agent.surface.option_sets == ["agent_set"]
        assert agent.surface.plugins == [{"Id": "Custom", "Source": "Gpt"}]
        # The whole connection is kept, not just the fields a surface cares about.
        assert agent.surface.query["scenario"] == "officeweb"

    def test_an_agent_does_not_touch_the_plain_surfaces(self):
        # The agent UI has no Work IQ toggle: letting its connection define the
        # `work` surface would send agent-shaped fields on every ordinary turn.
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL, socket_id=1)
        collector.note_frame_payload(
            1, chat_frame("", threadLevelGptId={"gptId": "x"}, optionsSets=["agent_set"])
        )
        assert collector.surfaces == {}
        assert collector.tones == {}

    def test_an_absent_tone_is_recorded_as_absent(self):
        collector = ProfileCollector()
        collector.note_frame_payload(1, chat_frame(None, threadLevelGptId={"gptId": "x"}))
        assert collector.agents["agent-1"].tone is None

    def test_the_same_agent_keeps_the_name_it_was_given(self):
        # A user renames `agent-1` in profile.json; a later capture must update that
        # entry rather than file the same agent again under a new name.
        collector = ProfileCollector()
        collector.note_frame_payload(1, chat_frame("", threadLevelGptId={"gptId": "x"}))
        collector.agents["sales-bot"] = collector.agents.pop("agent-1")

        seen = collector.note_frame_payload(
            1, chat_frame("", threadLevelGptId={"gptId": "x"}, optionsSets=["later"])
        )
        assert seen == []
        assert set(collector.agents) == {"sales-bot"}
        assert collector.agents["sales-bot"].surface.option_sets == ["later"]

    def test_two_agents_get_two_names(self):
        collector = ProfileCollector()
        collector.note_frame_payload(1, chat_frame("", threadLevelGptId={"gptId": "one"}))
        collector.note_frame_payload(2, chat_frame("", threadLevelGptId={"gptId": "two"}))
        assert set(collector.agents) == {"agent-1", "agent-2"}

    def test_an_empty_gpt_id_is_a_plain_turn(self):
        collector = ProfileCollector()
        collector.note_url(CHATHUB_URL, socket_id=1)
        collector.note_frame_payload(1, chat_frame("Claude_Opus", threadLevelGptId={}))
        assert collector.agents == {}
        assert collector.tones == {"claude-opus": "Claude_Opus"}


class TestApiRecorder:
    """Recording the site's own calls, for judging whether automation is possible."""

    def test_only_write_calls_to_microsoft_are_recorded(self, tmp_path):
        recorder = ApiRecorder(tmp_path / "calls.ndjson")
        assert recorder.note("POST", "https://m365.cloud.microsoft/api/gpts", "{}") is True
        assert recorder.note("GET", "https://m365.cloud.microsoft/api/gpts", None) is False
        assert recorder.note("POST", "https://example.invalid/collect", "{}") is False
        assert recorder.note("POST", "https://browser.events.data.microsoft.com/x", "{}") is False
        assert recorder.count == 1

    def test_credentials_are_stripped_from_a_recorded_url(self, tmp_path):
        recorder = ApiRecorder(tmp_path / "calls.ndjson")
        recorder.note("POST", "https://m365.cloud.microsoft/x?access_token=SECRET&id=7", "{}")
        written = (tmp_path / "calls.ndjson").read_text()
        assert "SECRET" not in written
        assert "id=7" in written

    def test_redaction_leaves_a_plain_url_alone(self):
        assert redact_url("https://m365.cloud.microsoft/x") == "https://m365.cloud.microsoft/x"


def test_build_produces_a_saveable_profile():
    collector = ProfileCollector()
    collector.note_url(CHATHUB_URL, socket_id=1)
    collector.note_frame_payload(1, chat_frame("Claude_Sonnet"))

    profile = collector.build()
    assert profile.is_empty is False
    # Tones are shared, surfaces are not.
    assert profile.tones == {"claude-sonnet": "Claude_Sonnet"}
    assert profile.surfaces["work"].query["agent"] == "work"


class TestTheWholeInvocation:
    """Recording every field, because the ones that mattered were unrecorded."""

    def test_the_whole_argument_is_kept_as_a_template(self):
        collector = ProfileCollector()
        collector.note_frame_payload(
            1,
            chat_frame(
                "Chat",
                threadLevelGptId={"id": "x"},
                gptDefinition={"instructions": "be terse"},
                streamingMode="Delta",
            ),
        )

        template = collector.agents["agent-1"].raw_argument
        assert template["gptDefinition"] == {"instructions": "be terse"}
        assert template["streamingMode"] == "Delta"

    def test_an_unfamiliar_field_is_reported_once(self):
        # This report is the whole point of re-capturing: it names the field that
        # was missing when the agent's instructions went unheeded.
        collector = ProfileCollector()
        frame = chat_frame("Chat", threadLevelGptId={"id": "x"}, gptDefinition={"a": 1})

        seen = collector.note_frame_payload(1, frame)
        assert ("field", "gptDefinition") in [(o.kind, o.name) for o in seen]
        assert collector.note_frame_payload(1, frame) == []

    def test_what_belongs_to_one_turn_is_not_kept(self):
        # The captured message is something the user typed into their own chat.
        collector = ProfileCollector()
        collector.note_frame_payload(
            1,
            frames.encode(
                {
                    "type": 4,
                    "target": "chat",
                    "invocationId": "0",
                    "arguments": [
                        {
                            "threadLevelGptId": {"id": "x"},
                            "message": {"text": "my private question", "requestId": "r-1"},
                            "clientInfo": {"clientSessionId": "s-1", "deviceOS": "Linux"},
                            "clientCorrelationId": "r-1",
                            "sessionId": "s-1",
                            "traceId": "r-1",
                            "isStartOfSession": True,
                        }
                    ],
                }
            ),
        )

        template = collector.agents["agent-1"].raw_argument
        assert "my private question" not in json.dumps(template)
        assert set(template) == {"threadLevelGptId", "message", "clientInfo"}
        assert template["message"] == {}
        assert template["clientInfo"] == {"deviceOS": "Linux"}
