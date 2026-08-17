"""The tenant profile: what the real web client sends, learned from the browser.

Every wire constant in `protocol.py` is a snapshot of one tenant at one moment —
the surface (`agent`/`scenario`/`licenseType`), the feature `variants`, and above
all the `tone` values that select a model. Microsoft changes all of them, and they
differ between an individual, an education and a work tenant. A wrong `tone` is not
a soft failure: the server rejects the turn outright.

So rather than guessing, `m365-copilot-proxy capture` watches the real chat client
and writes what it actually sends here. This module is the reader: when a profile
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


@dataclass
class TenantProfile:
    """What the real client was observed sending."""

    query: dict[str, str] = field(default_factory=dict)
    tones: dict[str, str] = field(default_factory=dict)
    option_sets: list[str] = field(default_factory=list)
    allowed_message_types: list[str] = field(default_factory=list)
    captured_at: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.query or self.tones or self.option_sets)

    def to_json(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at or datetime.now(tz=UTC).isoformat(),
            "query": self.query,
            "tones": self.tones,
            "option_sets": self.option_sets,
            "allowed_message_types": self.allowed_message_types,
        }

    @classmethod
    def from_json(cls, data: Any) -> TenantProfile:
        if not isinstance(data, dict):
            raise ValueError("profile must be a JSON object")
        captured_at = data.get("captured_at")
        return cls(
            query=_string_map(data.get("query")),
            tones=_string_map(data.get("tones")),
            option_sets=_string_list(data.get("option_sets")),
            allowed_message_types=_string_list(data.get("allowed_message_types")),
            captured_at=captured_at if isinstance(captured_at, str) else None,
        )


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


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
            "Loaded tenant profile: %d query fields, %d tones (captured %s)",
            len(profile.query),
            len(profile.tones),
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
