"""A scripted opening exchange, checked before the real turn is sent.

A declarative agent honours its instructions but does not always act on them: it
answers a question that needed a tool instead of calling one. Saying so in a message —
"when you read or write files or run commands, always use your tools" — makes it
comply, reliably enough that it is worth doing on every new conversation rather than
hoping.

So this sends that message as its own turn, and CHECKS the answer. A step can demand
a specific reply ("if you understood, answer only agente-ok"), and a conversation that
does not give it is abandoned for a fresh one, up to `attempts` times. Nothing else
in the proxy can tell whether a model understood its instructions; a scripted question
with a known answer can.

The script lives in `<config_dir>/priming.json` and decides for itself which models it
applies to, so there is no separate switch to keep in sync with it. Placeholders let
one script serve a deliberately generic agent: `{{tools_prompt}}` puts the client's
actual tool list in the opening turn, where it is fresh every time, instead of frozen
into the agent's instructions.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from m365_copilot_proxy.bizchat.session import BizChatError
from m365_copilot_proxy.config import get_settings

log = logging.getLogger(__name__)

FILENAME = "priming.json"

#: The model id that catches everything without an entry of its own.
ANY_MODEL = "*"

FAIL = "fail"
CONTINUE = "continue"

#: How the answer is compared, in the log and in the error.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class PrimingError(BizChatError):
    """The opening exchange never produced the expected answer.

    A `BizChatError` on purpose: the server already turns those into a 502 with the
    message intact, and the streaming paths already catch them.
    """


@dataclass
class Step:
    """One message of the script, and what proves the model took it in."""

    text: str
    #: Case-insensitive substring the answer must contain.
    expect: str = ""
    #: Regular expression the answer must match. Wins over `expect` when both are set.
    expect_regex: str = ""
    #: Shown in logs and errors, so a failure names something a human recognises.
    label: str = ""

    @property
    def is_checked(self) -> bool:
        return bool(self.expect or self.expect_regex)

    def accepts(self, answer: str) -> bool:
        """Whether this answer counts as understanding. Unchecked steps always pass."""
        if self.expect_regex:
            try:
                return re.search(self.expect_regex, answer, re.IGNORECASE | re.DOTALL) is not None
            except re.error as exc:
                log.warning("Ignoring an invalid expect_regex %r: %s", self.expect_regex, exc)
                return True
        if self.expect:
            return self.expect.strip().lower() in answer.lower()
        return True

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.expect_regex:
            return f"expect_regex {self.expect_regex!r}"
        if self.expect:
            return f"expect {self.expect!r}"
        return "unchecked step"

    @classmethod
    def from_json(cls, data: Any) -> Step | None:
        if isinstance(data, str):
            return cls(text=data)
        if not isinstance(data, dict):
            return None
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return cls(
            text=text,
            expect=data.get("expect") if isinstance(data.get("expect"), str) else "",
            expect_regex=(
                data.get("expect_regex") if isinstance(data.get("expect_regex"), str) else ""
            ),
            label=data.get("label") if isinstance(data.get("label"), str) else "",
        )


@dataclass
class Script:
    """The whole file: which models to prime, with what, and how hard to try."""

    models: dict[str, list[Step]] = field(default_factory=dict)
    attempts: int = 3
    on_failure: str = FAIL

    def steps_for(self, model: str | None) -> list[Step]:
        """The steps for a model id, falling back to `*`, or none at all.

        A model in neither place is not primed. That is the whole scoping rule: the
        file says who it applies to, so there is no second setting to disagree with.
        """
        exact = self.models.get(model or "")
        if exact is not None:
            return exact
        return self.models.get(ANY_MODEL, [])

    @property
    def fails_closed(self) -> bool:
        return self.on_failure != CONTINUE

    @classmethod
    def from_json(cls, data: Any) -> Script:
        if not isinstance(data, dict):
            raise ValueError("priming config must be a JSON object")

        models: dict[str, list[Step]] = {}
        raw_models = data.get("models")
        if isinstance(raw_models, dict):
            for name, raw_steps in raw_models.items():
                if not isinstance(name, str) or not isinstance(raw_steps, list):
                    continue
                steps = [step for raw in raw_steps if (step := Step.from_json(raw)) is not None]
                models[name] = steps

        attempts = data.get("attempts")
        on_failure = data.get("on_failure")
        return cls(
            models=models,
            attempts=attempts if isinstance(attempts, int) and attempts > 0 else 3,
            on_failure=on_failure if on_failure in (FAIL, CONTINUE) else FAIL,
        )


def config_path() -> Path:
    return get_settings().config_dir / FILENAME


_cache: Script | None = None
_cache_key: tuple[Path, float] | None = None


def load() -> Script:
    """Read the script, or an empty one when absent or unreadable.

    Absent is the normal state and means "prime nothing". A broken file must not take
    the proxy down with it either — it is logged and ignored, the same way a broken
    tenant profile is. Re-reads when the file changes, so an edit takes effect on the
    next conversation without a restart.
    """
    global _cache, _cache_key
    path = config_path()
    try:
        key = (path, path.stat().st_mtime)
    except OSError:
        _cache, _cache_key = Script(), None
        return _cache

    if _cache is not None and _cache_key == key:
        return _cache

    try:
        script = Script.from_json(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        log.warning("Ignoring unreadable priming config at %s: %s", path, exc)
        script = Script()
    else:
        log.info(
            "Loaded priming script: %d model(s), %d attempts, on_failure=%s",
            len(script.models),
            script.attempts,
            script.on_failure,
        )

    _cache, _cache_key = script, key
    return _cache


def reset_cache() -> None:
    global _cache, _cache_key
    _cache, _cache_key = None, None


def render(text: str, values: dict[str, str]) -> str:
    """Fill `{{placeholder}}`s, leaving unknown ones alone.

    A typo must not silently delete part of the message, and it must not stop a turn
    either — so the literal survives and the log names it once per render.
    """
    unknown: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        unknown.append(name)
        return match.group(0)

    filled = _PLACEHOLDER_RE.sub(substitute, text)
    if unknown:
        log.warning(
            "Priming step uses unknown placeholder(s) %s — sent as written. Known: %s",
            ", ".join(sorted(set(unknown))),
            ", ".join(sorted(values)),
        )
    return filled


def rendered_steps(steps: Sequence[Step], values: dict[str, str]) -> list[Step]:
    """The steps with their placeholders filled, dropping any that came out empty.

    A step that is nothing but `{{tools_prompt}}` costs nothing when the client
    declared no tools, instead of sending a blank message and asking the model to make
    sense of it.
    """
    out: list[Step] = []
    for step in steps:
        text = render(step.text, values).strip()
        if text:
            out.append(
                Step(
                    text=text,
                    expect=step.expect,
                    expect_regex=step.expect_regex,
                    label=step.label,
                )
            )
    return out


async def _ask(
    session: Any,
    step: Step,
    *,
    token: str,
    model: str,
    work_iq: bool | None,
    agent: Any,
) -> str:
    """Send one step and return the whole answer."""
    from m365_copilot_proxy.bizchat.session import TurnResult

    result = TurnResult()
    stream = session.chat(
        token=token,
        text=step.text,
        model=model,
        work_iq=work_iq,
        agent=agent,
        result=result,
    )
    return "".join([chunk async for chunk in stream])


async def run(
    session: Any,
    *,
    token: str,
    steps: Sequence[Step],
    script: Script,
    model: str,
    work_iq: bool | None = None,
    agent: Any = None,
) -> None:
    """Walk the script on a fresh conversation, retrying in a new one if it fails.

    Every step is a real turn: it spends one of the conversation's 600 messages and
    one round trip, and each retry spends them again. That is the price of knowing the
    model is listening before any real work is sent.

    Raises `PrimingError` when the script never passes and the config says `fail`.
    """
    if not steps:
        return

    for attempt in range(1, script.attempts + 1):
        failure = await _attempt(
            session, steps, token=token, model=model, work_iq=work_iq, agent=agent
        )
        if failure is None:
            log.info("Priming passed on attempt %d/%d", attempt, script.attempts)
            return

        step, answer = failure
        log.warning(
            "Priming attempt %d/%d failed at %s — answer was %r",
            attempt,
            script.attempts,
            step.describe(),
            _excerpt(answer),
        )
        if attempt < script.attempts:
            # A model that has already answered wrongly keeps that answer in its
            # context, so the retry needs a conversation with no memory of it.
            log.info(
                "Discarding conversation %s and opening another. It stays in your "
                "Copilot history — nothing here deletes a chat.",
                session.conversation_id,
            )
            session.reset_conversation()

    message = (
        f"Priming failed {script.attempts} time(s): the model never satisfied "
        f"{failure[0].describe()}. Last answer: {_excerpt(failure[1])!r}"
    )
    if script.fails_closed:
        raise PrimingError(message)
    log.warning("%s — sending the turn anyway (on_failure=continue).", message)


async def _attempt(
    session: Any,
    steps: Sequence[Step],
    *,
    token: str,
    model: str,
    work_iq: bool | None,
    agent: Any,
) -> tuple[Step, str] | None:
    """One pass over the script. Returns the step that failed, or None."""
    for index, step in enumerate(steps, start=1):
        answer = await _ask(session, step, token=token, model=model, work_iq=work_iq, agent=agent)
        log.info(
            "Priming step %d/%d (%s): %r",
            index,
            len(steps),
            step.describe(),
            _excerpt(answer),
        )
        if not step.accepts(answer):
            return step, answer
    return None


def _excerpt(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"
