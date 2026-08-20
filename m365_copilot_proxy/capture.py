"""Learn this tenant's wire profile by watching the real Copilot web client.

The built-in constants in `bizchat/protocol.py` describe one tenant at one moment.
Rather than guess how yours differs — and a wrong `tone` fails the turn outright —
this opens the actual chat in a browser and records what it sends: the Chathub URL's
query fields (which encode the licence surface) and the `type:4` chat invocations
(which carry the `tone` behind each entry in the model picker).

It also records **declarative agents**: open one of your custom agents, send a
message, and the invocation's `threadLevelGptId` — the opaque object that puts a
thread inside that agent — is kept whole, so the proxy can re-enter it later. An
agent brings its own connection shape, so nothing it sends is filed under the plain
`work`/`web` surfaces.

The user drives it: pick a model or an agent, send any message, repeat. Every
observation is additive, so capture can be re-run later to pick up a newly offered
model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from m365_copilot_proxy.bizchat import frames, protocol
from m365_copilot_proxy.bizchat.profile import (
    CAPTURED_QUERY_KEYS,
    PER_CONNECTION_QUERY_KEYS,
    WEB,
    DeclarativeAgent,
    Surface,
    TenantProfile,
    slug_for_tone,
)
from m365_copilot_proxy.config import get_settings

log = logging.getLogger(__name__)

CHAT_URL = "https://m365.cloud.microsoft/chat"

#: Slug given to a newly seen agent, numbered. The name is not on the wire — the
#: user renames it in `profile.json`, and a re-capture recognises the agent by its
#: id rather than by this name, so the rename survives.
AGENT_SLUG_PREFIX = "agent-"


class CaptureError(RuntimeError):
    """The capture session could not be started."""


def _notice(message: str) -> None:
    print(f"[capture] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Observation:
    """Something learned for the first time, for the caller to report.

    `kind` is `tone`, `surface` or `agent`; `name` is the tone, the surface name or
    the agent slug.
    """

    kind: str
    name: str


@dataclass
class ProfileCollector:
    """Accumulates observations. Owned by the caller so a Ctrl-C still saves.

    Observations are filed per surface, because the "Work IQ" toggle swaps the whole
    client shape at once — `agent`, `scenario`, `variants` and the entire
    `optionsSets` family. The surface names itself: the `agent` field of the
    connection URL says which one this is, so running capture once per toggle state
    fills both slots with no flag to remember.

    Nothing is filed until a turn is actually sent. A connection alone does not say
    what it is for: only the chat invocation reveals whether the thread belongs to a
    plain surface or to a declarative agent, and an agent's connection must not
    overwrite the surface a normal turn will use.
    """

    surfaces: dict[str, Surface] = field(default_factory=dict)
    #: Tones are shared: the toggle changes the surface, not which models exist.
    tones: dict[str, str] = field(default_factory=dict)
    #: Slug -> declarative agent, keyed by a name the user may rename.
    agents: dict[str, DeclarativeAgent] = field(default_factory=dict)
    #: Per-socket leftovers, since a frame can straddle two WebSocket messages.
    _buffers: dict[int, str] = field(default_factory=dict, repr=False)
    #: The query each live socket was opened with, minus the per-connection fields.
    _socket_query: dict[int, dict[str, str]] = field(default_factory=dict, repr=False)
    #: Unfamiliar invocation fields already reported, so each is mentioned once.
    _reported_fields: set[str] = field(default_factory=set, repr=False)

    @property
    def observations(self) -> int:
        return len(self.tones) + len(self.agents)

    def note_url(self, url: str, socket_id: int = 0) -> str | None:
        """Stash a Chathub URL's query fields. Returns the surface it names, or None.

        `access_token` is never read: it is a credential, it is per-connection, and
        it has no business being written to a config file. Neither are the ids the
        client mints per turn — replaying those would be nonsense.
        """
        parsed = urlparse(url)
        if protocol.WS_PATH.lower() not in parsed.path.lower():
            return None
        params = parse_qs(parsed.query)
        self._socket_query[socket_id] = {
            key: values[0]
            for key, values in params.items()
            if key not in PER_CONNECTION_QUERY_KEYS and values and values[0]
        }
        return self._socket_query[socket_id].get("agent", WEB)

    def note_frame_payload(self, socket_id: int, payload: str) -> list[Observation]:
        """Feed one raw WebSocket payload; returns what it taught us."""
        buffer = self._buffers.get(socket_id, "") + payload
        complete, rest = frames.split_frames(buffer)
        self._buffers[socket_id] = rest

        seen: list[Observation] = []
        for chunk in complete:
            frame = frames.parse(chunk)
            if not frame or frame.get("type") != frames.TYPE_CLIENT_INVOCATION:
                continue
            if frame.get("target") != "chat":
                continue
            arguments = frame.get("arguments")
            if not isinstance(arguments, list) or not arguments:
                continue
            argument = arguments[0]
            if not isinstance(argument, dict):
                continue

            gpt_id = argument.get("threadLevelGptId")
            if isinstance(gpt_id, dict) and gpt_id:
                seen += self._note_agent(socket_id, argument, gpt_id)
            else:
                seen += self._note_surface(socket_id, argument)
        return seen

    def _note_surface(self, socket_id: int, argument: dict[str, Any]) -> list[Observation]:
        query = self._socket_query.get(socket_id, {})
        name = query.get("agent", WEB)
        seen = [] if name in self.surfaces else [Observation("surface", name)]

        surface = self.surfaces.setdefault(name, Surface())
        surface.query.update({k: v for k, v in query.items() if k in CAPTURED_QUERY_KEYS})
        _absorb_surface(surface, argument)

        tone = argument.get("tone")
        if isinstance(tone, str) and tone:
            model_id = slug_for_tone(tone)
            if self.tones.get(model_id) != tone:
                self.tones[model_id] = tone
                seen.append(Observation("tone", tone))
        return seen

    def _note_agent(
        self, socket_id: int, argument: dict[str, Any], gpt_id: dict[str, Any]
    ) -> list[Observation]:
        """File a turn that ran inside a declarative agent.

        The whole invocation is kept, not a chosen list of fields. Replaying only the
        fields someone thought of reached the agent's thread but was answered by plain
        Copilot: whatever asks for the agent's instructions is something else in there.
        So the argument is stored as a template, and any field in it the proxy does not
        build itself is reported — that report is the point of re-capturing.
        """
        slug = self._slug_for(gpt_id)
        seen = [] if slug in self.agents else [Observation("agent", slug)]

        agent = self.agents.setdefault(slug, DeclarativeAgent())
        agent.thread_level_gpt_id = dict(gpt_id)
        agent.surface.query.update(self._socket_query.get(socket_id, {}))
        _absorb_surface(agent.surface, argument)
        agent.raw_argument = strip_per_turn_fields(argument)

        extras = argument.get("extraExtensionParameters")
        if isinstance(extras, dict):
            agent.extra_extension_parameters = dict(extras)
        source = argument.get("source")
        if isinstance(source, str) and source:
            agent.source = source
        # The agent UI has no model picker, so whatever tone it sends is the only
        # correct one — and an absent tone is recorded as absent, not filled in.
        tone = argument.get("tone")
        agent.tone = tone if isinstance(tone, str) and tone else None

        for name in sorted(set(argument) - MANAGED_ARGUMENT_KEYS):
            if name not in self._reported_fields:
                self._reported_fields.add(name)
                seen.append(Observation("field", name))
        return seen

    def _slug_for(self, gpt_id: dict[str, Any]) -> str:
        """The name to file an agent under, reusing the one it already has.

        Matching on the id rather than the name is what lets a user rename an agent
        in `profile.json` and still have a later capture update that same entry.
        """
        for slug, agent in self.agents.items():
            if agent.thread_level_gpt_id == gpt_id:
                return slug
        index = len(self.agents) + 1
        while f"{AGENT_SLUG_PREFIX}{index}" in self.agents:
            index += 1
        return f"{AGENT_SLUG_PREFIX}{index}"

    def build(self) -> TenantProfile:
        return TenantProfile(
            surfaces={name: s for name, s in self.surfaces.items() if not s.is_empty},
            tones=dict(self.tones),
            agents={name: a for name, a in self.agents.items() if not a.is_empty},
        )


#: Invocation fields the proxy builds for itself on every turn. Anything an agent
#: sends outside this set is news — and news is what re-capturing is for.
MANAGED_ARGUMENT_KEYS = frozenset(
    {
        "source",
        "clientCorrelationId",
        "sessionId",
        "optionsSets",
        "streamingMode",
        "spokenTextMode",
        "options",
        "extraExtensionParameters",
        "allowedMessageTypes",
        "sliceIds",
        "threadLevelGptId",
        "traceId",
        "isStartOfSession",
        "clientInfo",
        "message",
        "plugins",
        "isSbsSupported",
        "tone",
        "renderReferencesBehindEOS",
        "disconnectBehavior",
    }
)

#: Argument fields that belong to one turn and are regenerated for every later one.
_PER_TURN_KEYS = ("clientCorrelationId", "sessionId", "traceId", "isStartOfSession")

#: The same, inside `message`. `text` is the user's own message: it must not be
#: written to a config file, and it would be overwritten on replay anyway.
_PER_TURN_MESSAGE_KEYS = ("text", "requestId")


def strip_per_turn_fields(argument: dict[str, Any]) -> dict[str, Any]:
    """A copy of an invocation with everything turn-specific taken out.

    What is left is a template: the shape of the client's request, without the ids of
    the turn that carried it or the words the user happened to type.
    """
    template = deepcopy(argument)
    for key in _PER_TURN_KEYS:
        template.pop(key, None)

    message = template.get("message")
    if isinstance(message, dict):
        for key in _PER_TURN_MESSAGE_KEYS:
            message.pop(key, None)

    client_info = template.get("clientInfo")
    if isinstance(client_info, dict):
        client_info.pop("clientSessionId", None)
    return template


def _absorb_surface(surface: Surface, argument: dict[str, Any]) -> None:
    """Copy the fields a turn declares into the surface that sent it."""
    for key, target in (
        ("optionsSets", surface.option_sets),
        ("allowedMessageTypes", surface.allowed_message_types),
    ):
        values = argument.get(key)
        if isinstance(values, list):
            target[:] = [v for v in values if isinstance(v, str)]
    sent_plugins = argument.get("plugins")
    if isinstance(sent_plugins, list):
        surface.plugins = [p for p in sent_plugins if isinstance(p, dict)]


def describe(observation: Observation) -> str:
    if observation.kind == "tone":
        return (
            f"tone captured: {observation.name}  ->  "
            f"model id `{slug_for_tone(observation.name)}`"
        )
    if observation.kind == "agent":
        return (
            f"declarative agent captured  ->  model id "
            f"`{protocol.AGENT_ID_PREFIX}{observation.name}`"
        )
    if observation.kind == "field":
        return f"the agent also sends `{observation.name}` — recorded, and replayed with it"
    return f"recording the '{observation.name}' surface"


# --- Recording the agent builder's own traffic -------------------------------
#
# Creating an agent is a browser flow with no documented API, so the only way to
# judge whether the proxy could ever push instructions into one is to look at what
# the builder actually sends. This records that and nothing else: it is evidence to
# read, never something replayed.

#: Hosts that are telemetry, not the product.
_TELEMETRY_HINTS = ("events.data.microsoft.com", "aria", "telemetry", "otlp", "browser.pipe")

#: Query fields that carry credentials and are stripped from a recorded URL.
_SECRET_QUERY_HINTS = ("token", "code", "secret", "key", "assertion")

#: Only Microsoft's own hosts are recorded, so an unrelated tab cannot end up here.
_MICROSOFT_HOSTS = (".microsoft.com", ".office.com", ".cloud.microsoft", ".live.com")


def _is_interesting(method: str, url: str) -> bool:
    """A state-changing call to something that is not telemetry."""
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not any(host == suffix[1:] or host.endswith(suffix) for suffix in _MICROSOFT_HOSTS):
        return False
    return not any(hint in host for hint in _TELEMETRY_HINTS)


def redact_url(url: str) -> str:
    """The URL with any credential-shaped query field blanked."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = []
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        secret = any(hint in key.lower() for hint in _SECRET_QUERY_HINTS)
        kept += [f"{key}=REDACTED" if secret else f"{key}={value}" for value in values]
    return parsed._replace(query="&".join(kept)).geturl()


class ApiRecorder:
    """Appends the agent builder's write calls to an NDJSON file.

    Headers are never recorded — that is where the bearer token lives. Bodies are,
    because the body is the point: it is what would have to be sent to create or
    update an agent. Treat the file as containing your agent's text.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.count = 0

    def note(self, method: str, url: str, body: str | None) -> bool:
        if not _is_interesting(method, url):
            return False
        entry = {
            "at": datetime.now(tz=UTC).isoformat(),
            "method": method,
            "url": redact_url(url),
            "body": body,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.debug("Could not record %s %s: %s", method, url, exc)
            return False
        self.count += 1
        return True


def api_recorder_path() -> Path:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return get_settings().capture_dir / f"agent-builder-{stamp}.ndjson"


async def run(collector: ProfileCollector, *, record_api: bool = False) -> ApiRecorder | None:
    """Open the real chat and watch it until the window closes or Ctrl-C."""
    from playwright.async_api import async_playwright

    from m365_copilot_proxy.auth.login import browser_launch_kwargs

    settings = get_settings()
    settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    finished = asyncio.Event()
    recorder = ApiRecorder(api_recorder_path()) if record_api else None

    def on_websocket(ws: Any) -> None:
        socket_id = id(ws)
        if collector.note_url(ws.url, socket_id) is None:
            return

        def on_frame_sent(payload: Any) -> None:
            if not isinstance(payload, str):
                return  # binary frames are not part of this protocol
            for observation in collector.note_frame_payload(socket_id, payload):
                _notice(describe(observation))

        ws.on("framesent", on_frame_sent)

    def on_request(request: Any) -> None:
        if recorder is None:
            return
        try:
            recorder.note(request.method, request.url, request.post_data)
        except Exception as exc:  # a recorder must never break the capture
            log.debug("Could not read a request: %s", exc)

    async with async_playwright() as pw:
        try:
            context = await pw.chromium.launch_persistent_context(
                str(settings.browser_profile_dir), **browser_launch_kwargs()
            )
        except Exception as exc:
            raise CaptureError(
                f"Could not launch Chromium ({exc}). "
                "Install the browser with `playwright install chromium`, "
                "or point M365_CHROMIUM_PATH at an existing one."
            ) from exc

        try:
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("websocket", on_websocket)
            if recorder is not None:
                context.on("request", on_request)
            page.on("close", lambda _: finished.set())
            context.on("close", lambda _: finished.set())

            _notice("Opening Microsoft 365 Copilot.")
            _notice("For each model you want to use: pick it in the model selector,")
            _notice("send any short message, and wait for the reply to start.")
            _notice("Run this once with Work IQ on and once with it off to record")
            _notice("both surfaces — each run keeps the other one.")
            _notice("To use one of your agents from the proxy, open it and send a")
            _notice("message too — it is captured as its own model id.")
            if recorder is not None:
                _notice(f"Recording the site's write calls to {recorder.path}")
            _notice("Close the window (or press Ctrl-C) when you are done.")
            await page.goto(CHAT_URL, wait_until="domcontentloaded")

            await finished.wait()
        finally:
            try:
                await context.close()
            except Exception as exc:
                log.debug("Browser already closed: %s", exc)
    return recorder
