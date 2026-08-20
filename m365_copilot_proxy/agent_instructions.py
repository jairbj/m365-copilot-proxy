"""Exporting what the proxy tells Copilot, for pasting into a declarative agent.

M365 Copilot often ignores the `[System instructions]` block the proxy glues into
the first turn — but it honours the instructions of a declarative agent built in the
Copilot UI. So the practical fix is to move that text into an agent, and this module
is what makes it possible: it records the system prompt each client sends, composes
it with the tool contract into one document, and measures the result against the
agent's 8000-character instructions field.

Nothing here truncates. A document over the limit is reported, never silently cut:
which paragraph to drop is a judgement call, and making it quietly would leave an
agent that disagrees with the prompt in ways nobody can see.
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
from m365_copilot_proxy.openai_api.tools import TOOL_CONTRACT

log = logging.getLogger(__name__)

#: The hard cap on a declarative agent's instructions field.
INSTRUCTIONS_LIMIT = 8000

#: How many recorded prompts to keep. One per conversation fingerprint, and a
#: harness opens a lot of conversations — but only the recent ones are ever read.
KEEP_RECORDS = 20

DIRNAME = "agent-instructions"

CONTRACT_TITLE = "Tool calling"
TOOLS_TITLE = "Available tools"
PROMPT_TITLE = "System prompt"


def records_dir() -> Path:
    return get_settings().config_dir / DIRNAME


@dataclass
class Section:
    """One titled part of the document, so the caller can see what costs what."""

    title: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Document:
    """The instructions to paste, plus what they weigh."""

    sections: list[Section] = field(default_factory=list)
    #: Where the system prompt came from, when it came from a recording.
    key: str | None = None
    model: str | None = None
    label: str | None = None
    recorded_at: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.sections if s.text).strip()

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def over_by(self) -> int:
        """Characters past the agent's field limit; 0 when it fits."""
        return max(0, self.chars - INSTRUCTIONS_LIMIT)

    @property
    def fits(self) -> bool:
        return self.over_by == 0

    def breakdown(self) -> list[tuple[str, int]]:
        return [(s.title, s.chars) for s in self.sections if s.text]

    def to_json(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "chars": self.chars,
            "limit": INSTRUCTIONS_LIMIT,
            "over_by": self.over_by,
            "fits": self.fits,
            "sections": [{"title": t, "chars": n} for t, n in self.breakdown()],
            "source": {
                "key": self.key,
                "model": self.model,
                "label": self.label,
                "recorded_at": self.recorded_at,
            },
        }


def compose(
    system_text: str = "",
    *,
    contract: bool = True,
    tools: bool = True,
    prompt: bool = True,
    tool_text: str = "",
    key: str | None = None,
    model: str | None = None,
    label: str | None = None,
    recorded_at: str | None = None,
) -> Document:
    """Build the document to paste into the agent's instructions field.

    In the order a model needs them: how to call a tool, what there is to call, and
    then the client's own prompt. A turn bound to an agent carries none of the three,
    so what is not pasted here is not sent at all. Any part can be left out —
    `--contract`, `--tools` and `--raw` on the CLI.
    """
    sections: list[Section] = []
    if contract:
        sections.append(Section(CONTRACT_TITLE, TOOL_CONTRACT))
    if tools and tool_text.strip():
        sections.append(Section(TOOLS_TITLE, tool_text.strip()))
    if prompt and system_text.strip():
        sections.append(Section(PROMPT_TITLE, system_text.strip()))
    return Document(
        sections=sections, key=key, model=model, label=label, recorded_at=recorded_at
    )


@dataclass
class Record:
    """What one conversation asked the model to be: its prompt and its tools."""

    key: str
    system_text: str
    #: The `[Available tools]` block as the client's tools render, contract excluded.
    #: Empty for a record made before agents took the tool list over.
    tool_text: str = ""
    model: str | None = None
    label: str | None = None
    recorded_at: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.system_text.strip() or self.tool_text.strip())

    def compose(self, **kwargs: Any) -> Document:
        return compose(
            self.system_text,
            tool_text=self.tool_text,
            key=self.key,
            model=self.model,
            label=self.label,
            recorded_at=self.recorded_at,
            **kwargs,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "model": self.model,
            "label": self.label,
            "recorded_at": self.recorded_at,
            "system_text": self.system_text,
            "tool_text": self.tool_text,
        }

    @classmethod
    def from_json(cls, data: Any) -> Record | None:
        if not isinstance(data, dict):
            return None
        key, text = data.get("key"), data.get("system_text")
        if not isinstance(key, str) or not isinstance(text, str):
            return None
        return cls(
            key=key,
            system_text=text,
            tool_text=data.get("tool_text") if isinstance(data.get("tool_text"), str) else "",
            model=data.get("model") if isinstance(data.get("model"), str) else None,
            label=data.get("label") if isinstance(data.get("label"), str) else None,
            recorded_at=(
                data.get("recorded_at") if isinstance(data.get("recorded_at"), str) else None
            ),
        )


_SAFE_KEY = re.compile(r"[^a-zA-Z0-9_-]")


def _path_for(key: str) -> Path:
    return records_dir() / f"{_SAFE_KEY.sub('_', key)[:64]}.json"


def record(
    key: str,
    system_text: str,
    *,
    tool_text: str = "",
    model: str | None = None,
    label: str = "",
) -> Path | None:
    """Remember what a conversation that is starting told the model to be.

    Called on every new conversation, so it is deliberately cheap and quiet: an
    unchanged record is not rewritten, and a failure to write is logged rather than
    raised — losing a copy of the prompt must never cost a turn.

    A client that declares tools but sends no system prompt is still worth recording:
    for an agent, that list is the part that has to be pasted.
    """
    entry = Record(
        key=key,
        system_text=system_text,
        tool_text=tool_text,
        model=model,
        label=(label or "").strip()[:120] or None,
        recorded_at=datetime.now(tz=UTC).isoformat(),
    )
    if not get_settings().record_system_prompts or entry.is_empty:
        return None

    path = _path_for(key)
    try:
        existing = load(key)
        if existing is not None and (existing.system_text, existing.tool_text) == (
            system_text,
            tool_text,
        ):
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(entry.to_json(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _prune()
    except OSError as exc:
        log.debug("Could not record the system prompt: %s", exc)
        return None
    return path


def _prune() -> None:
    files = sorted(records_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[KEEP_RECORDS:]:
        stale.unlink(missing_ok=True)


def load(key: str) -> Record | None:
    return _read(_path_for(key))


def _read(path: Path) -> Record | None:
    try:
        return Record.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Ignoring unreadable prompt record %s: %s", path, exc)
        return None


def list_records() -> list[Record]:
    """Everything recorded, most recent first."""
    try:
        paths = sorted(
            records_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return []
    return [entry for path in paths if (entry := _read(path)) is not None]


def latest() -> Record | None:
    records = list_records()
    return records[0] if records else None
