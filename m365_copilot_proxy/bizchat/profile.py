"""The tenant profile: what the real web client sends, learned from the browser.

Every wire constant in `protocol.py` is a snapshot of one tenant at one moment —
the licence surface, the feature `variants`, the `optionsSets`, and above all the
`tone` values that select a model. Microsoft changes all of them, and they differ
between an individual, an education and a work tenant. A wrong `tone` is not a soft
failure: the server rejects the turn outright.

The web client also has more than one shape at a time. The "Work IQ" toggle does
not flip a single field — it swaps a whole surface: `agent`, `scenario`, the
`variants` string and the entire `optionsSets` family all change together
(`enterprise_*` with work grounding on, `cwc_*` with it off). So a profile holds one
entry per surface, and the proxy serves whichever the caller asked for rather than
mixing fields from both.

The same file also holds the tenant's **declarative agents** — the custom agents
built in the Copilot UI, which the proxy can start a thread inside. Careful with the
word: `agent` as a surface name below means the Work IQ toggle (`work`/`web`), while
a `DeclarativeAgent` is the custom agent a `threadLevelGptId` selects.

`m365-copilot-proxy capture` writes this file; this module reads it. When a profile
exists it wins over the built-in defaults, and when it does not, nothing changes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from m365_copilot_proxy.config import get_settings

log = logging.getLogger(__name__)

PROFILE_FILENAME = "profile.json"

#: The two surfaces, named by the `agent` query field that identifies them.
WORK = "work"
WEB = "web"

#: Query-string fields worth learning. `access_token` is deliberately absent — it
#: is a credential, it is per-connection, and it must never reach the profile.
CAPTURED_QUERY_KEYS = (
    "source",
    "product",
    "agentHost",
    "licenseType",
    "agent",
    "scenario",
    "isEdu",
    "variants",
)

#: Query fields that belong to ONE connection and must never be replayed: the
#: credential, and the ids the proxy mints per turn. A declarative agent records
#: every other field it is seen with, since which one carries the agent is not
#: something we can know in advance.
PER_CONNECTION_QUERY_KEYS = frozenset(
    {"access_token", "chatsessionid", "clientrequestid", "X-SessionId", "ConversationId"}
)

#: Recorded fields that name a session rather than a setting. The capture keeps the
#: value it saw — it is evidence that this connection carries the field at all — but
#: replaying it would pin every future conversation to a session that ended when the
#: capture window closed, so the value is minted fresh per conversation instead.
REFRESHED_QUERY_KEYS = frozenset({"XRoutingParameterSessionKey"})


@dataclass
class Surface:
    """One complete shape of the client: everything that changes as a unit."""

    query: dict[str, str] = field(default_factory=dict)
    option_sets: list[str] = field(default_factory=list)
    allowed_message_types: list[str] = field(default_factory=list)
    #: None means the capture never saw them; [] means it saw none.
    plugins: list[dict[str, Any]] | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.query
            or self.option_sets
            or self.allowed_message_types
            or self.plugins is not None
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "option_sets": self.option_sets,
            "allowed_message_types": self.allowed_message_types,
            "plugins": self.plugins,
        }

    @classmethod
    def from_json(cls, data: Any) -> Surface:
        if not isinstance(data, dict):
            return cls()
        return cls(
            query=_string_map(data.get("query")),
            option_sets=_string_list(data.get("option_sets")),
            allowed_message_types=_string_list(data.get("allowed_message_types")),
            plugins=(
                [p for p in raw_plugins if isinstance(p, dict)]
                if isinstance(raw_plugins := data.get("plugins"), list)
                else None
            ),
        )


@dataclass
class DeclarativeAgent:
    """A custom agent from the Copilot UI, recorded well enough to re-enter it.

    The field that selects one (`threadLevelGptId`) is opaque and undocumented, so
    nothing here interprets it: capture records what the real client sent and the
    session replays it verbatim. The id shows up in the connection's query too, which
    is why the whole surface is kept rather than only the invocation.

    An agent has no model picker and no Work IQ toggle, so its `tone` is whatever its
    own client was seen sending — `Chat`, on the tenant this was captured from — and
    not something the caller chooses.

    Recording a chosen list of fields turned out not to be enough: a turn replayed
    from those alone reaches the agent's thread but is answered by plain Copilot,
    which ignores the agent's instructions. Whatever asks for them is a field nobody
    thought to record. So `raw_argument` keeps the WHOLE invocation the client sent,
    minus the parts that belong to one turn, and the session replays it as a template.
    """

    surface: Surface = field(default_factory=Surface)
    thread_level_gpt_id: dict[str, Any] = field(default_factory=dict)
    extra_extension_parameters: dict[str, Any] = field(default_factory=dict)
    #: The tone the agent's own client sends. None means it sent none, and so do we.
    tone: str | None = None
    source: str | None = None
    #: The complete captured invocation, per-turn fields stripped. Empty for a profile
    #: captured before this existed — re-run `capture` to fill it in.
    raw_argument: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """An agent with no id cannot be entered, whatever else was recorded."""
        return not self.thread_level_gpt_id

    def to_json(self) -> dict[str, Any]:
        return {
            "surface": self.surface.to_json(),
            "thread_level_gpt_id": self.thread_level_gpt_id,
            "extra_extension_parameters": self.extra_extension_parameters,
            "tone": self.tone,
            "source": self.source,
            "raw_argument": self.raw_argument,
        }

    @classmethod
    def from_json(cls, data: Any) -> DeclarativeAgent:
        if not isinstance(data, dict):
            return cls()
        tone, source = data.get("tone"), data.get("source")
        return cls(
            surface=Surface.from_json(data.get("surface")),
            thread_level_gpt_id=_json_map(data.get("thread_level_gpt_id")),
            extra_extension_parameters=_json_map(data.get("extra_extension_parameters")),
            tone=tone if isinstance(tone, str) and tone else None,
            source=source if isinstance(source, str) and source else None,
            raw_argument=_json_map(data.get("raw_argument")),
        )


@dataclass
class TenantProfile:
    """What the real client was observed sending, per surface."""

    #: Surface name (`work` / `web`) -> what that surface sends.
    surfaces: dict[str, Surface] = field(default_factory=dict)
    #: Model tones are a choice of model, not of surface, so they are shared.
    tones: dict[str, str] = field(default_factory=dict)
    #: Slug -> declarative agent, exposed as the `agent:<slug>` model ids.
    agents: dict[str, DeclarativeAgent] = field(default_factory=dict)
    captured_at: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.tones
            or any(not s.is_empty for s in self.surfaces.values())
            or any(not a.is_empty for a in self.agents.values())
        )

    def surface_for(self, name: str) -> Surface | None:
        """The requested surface, or the only one we have, or None.

        Falling back to the other surface is deliberate: half a profile is still
        better than the inherited defaults, which describe a different tenant
        entirely. The caller logs the substitution so it is never silent.
        """
        exact = self.surfaces.get(name)
        if exact is not None and not exact.is_empty:
            return exact
        usable = [s for s in self.surfaces.values() if not s.is_empty]
        return usable[0] if len(usable) == 1 else None

    def has_surface(self, name: str) -> bool:
        surface = self.surfaces.get(name)
        return surface is not None and not surface.is_empty

    def to_json(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at or datetime.now(tz=UTC).isoformat(),
            "tones": self.tones,
            "surfaces": {name: s.to_json() for name, s in self.surfaces.items()},
            "agents": {name: a.to_json() for name, a in self.agents.items()},
        }

    @classmethod
    def from_json(cls, data: Any) -> TenantProfile:
        if not isinstance(data, dict):
            raise ValueError("profile must be a JSON object")
        captured_at = data.get("captured_at")
        surfaces: dict[str, Surface] = {}

        raw_surfaces = data.get("surfaces")
        if isinstance(raw_surfaces, dict):
            for name, raw in raw_surfaces.items():
                if isinstance(name, str):
                    surfaces[name] = Surface.from_json(raw)
        elif any(k in data for k in ("query", "option_sets", "allowed_message_types", "plugins")):
            # A profile captured before surfaces existed. It described whichever
            # surface the toggle happened to be in, and the `agent` field says
            # which — so it migrates itself into the right slot.
            legacy = Surface.from_json(data)
            surfaces[legacy.query.get("agent", WEB)] = legacy

        agents: dict[str, DeclarativeAgent] = {}
        raw_agents = data.get("agents")
        if isinstance(raw_agents, dict):
            for name, raw in raw_agents.items():
                if isinstance(name, str):
                    agents[name] = DeclarativeAgent.from_json(raw)

        return cls(
            surfaces=surfaces,
            tones=_string_map(data.get("tones")),
            agents={name: a for name, a in agents.items() if not a.is_empty},
            captured_at=captured_at if isinstance(captured_at, str) else None,
        )


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def _json_map(value: Any) -> dict[str, Any]:
    """A JSON object with values left untouched — the agent fields are opaque."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str)}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def profile_path() -> Path:
    return get_settings().config_dir / PROFILE_FILENAME


_cache: TenantProfile | None = None
_cache_key: tuple[Path, float] | None = None


def load() -> TenantProfile:
    """Read the profile, or an empty one when absent.

    A missing profile is the normal state, and a broken one must not take the proxy
    down with it — either way we fall back to the built-in defaults and say so.
    Re-reads when the file changes on disk, so a fresh capture takes effect without
    restarting the server.
    """
    global _cache, _cache_key
    path = profile_path()
    try:
        key = (path, path.stat().st_mtime)
    except OSError:
        _cache, _cache_key = TenantProfile(), None
        return _cache

    if _cache is not None and _cache_key == key:
        return _cache

    try:
        profile = TenantProfile.from_json(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        log.warning("Ignoring unreadable tenant profile at %s: %s", path, exc)
        profile = TenantProfile()
    else:
        log.info(
            "Loaded tenant profile: surfaces=%s, %d tones, %d agents (captured %s)",
            sorted(name for name in profile.surfaces if profile.has_surface(name)),
            len(profile.tones),
            len(profile.agents),
            profile.captured_at,
        )

    _cache, _cache_key = profile, key
    return profile


def save(profile: TenantProfile) -> Path:
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    reset_cache()
    return path


def reset_cache() -> None:
    global _cache, _cache_key
    _cache, _cache_key = None, None


def slug_for_tone(tone: str) -> str:
    """Turn a server tone into a model id a user would plausibly type.

    `Gpt_5_6_Quick` -> `gpt-5.6-quick`, `Claude_Sonnet` -> `claude-sonnet`. Version
    segments are rejoined with dots because that is how the models are named
    everywhere else (`gpt-5.6`, not `gpt-5-6`).
    """
    parts = [p for p in re.split(r"[_\s]+", tone.strip()) if p]
    if not parts:
        return tone.strip().lower()

    out: list[str] = []
    for part in parts:
        lowered = part.lower()
        # Merge a run of bare numbers into a dotted version: 5, 6 -> 5.6
        if lowered.isdigit() and out and re.fullmatch(r"[\d.]+", out[-1]):
            out[-1] = f"{out[-1]}.{lowered}"
        else:
            out.append(lowered)
    return "-".join(out)
