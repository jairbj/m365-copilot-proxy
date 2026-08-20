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

import difflib
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

#: Everything a step may say. Anything else is a typo, and a typo that is skipped in
#: silence is how a two-step script quietly becomes a one-step script.
STEP_KEYS = ("text", "expect", "expect_regex", "label")

#: The same, for the top level: `atempts` must not quietly mean the default.
SCRIPT_KEYS = ("models", "attempts", "on_failure")


class PrimingError(BizChatError):
    """The opening exchange never produced the expected answer.

    A `BizChatError` on purpose: the server already turns those into a 502 with the
    message intact, and the streaming paths already catch them.
    """


class PrimingConfigError(PrimingError):
    """The script itself cannot be read, so no conversation can be primed from it.

    Refusing the turn is the point. Running the steps that happen to parse would leave
    a conversation primed for some of what the script says and not the rest, with a
    checked reply still passing — a failure indistinguishable from success.
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


def unknown_key_problem(keys: Sequence[str], valid: Sequence[str]) -> str:
    """Name the misspelt keys, and guess what they were meant to be.

    The guess is the whole value of this message: `texto` for `text` is a one-second
    fix once someone says it out loud, and a lost afternoon otherwise.
    """
    parts = []
    for key in keys:
        close = difflib.get_close_matches(key, valid, n=1)
        hint = f" (did you mean `{close[0]}`?)" if close else ""
        parts.append(f"unknown key `{key}`{hint}")
    return f"{'; '.join(parts)} — valid keys are {', '.join(f'`{k}`' for k in valid)}"


def parse_step(data: Any) -> tuple[Step | None, str]:
    """One step, or the reason it cannot be read.

    Never returns a half-understood step: a dict with an unknown key is refused even
    when it also has a usable `text`, because `{"text": ..., "expects": "ok"}` would
    otherwise run as a step nobody checks.
    """
    if isinstance(data, str):
        return (Step(text=data), "") if data.strip() else (None, "is an empty string")
    if not isinstance(data, dict):
        return None, f"is a {type(data).__name__}, not an object"

    unknown = [key for key in data if key not in STEP_KEYS]
    if unknown:
        return None, unknown_key_problem(unknown, STEP_KEYS)

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        present = ", ".join(f"`{k}`" for k in data) or "none"
        return None, f"has no usable `text` (keys present: {present})"

    for name in ("expect", "expect_regex", "label"):
        value = data.get(name)
        if value is not None and not isinstance(value, str):
            return None, f"`{name}` must be a string, not a {type(value).__name__}"

    return (
        Step(
            text=text,
            expect=data.get("expect") or "",
            expect_regex=data.get("expect_regex") or "",
            label=data.get("label") or "",
        ),
        "",
    )


@dataclass
class Script:
    """The whole file: which models to prime, with what, and how hard to try."""

    models: dict[str, list[Step]] = field(default_factory=dict)
    attempts: int = 3
    on_failure: str = FAIL
    #: What could not be read, by model id — `""` for problems with the file itself.
    #: A script with problems is refused rather than partly run.
    problems: dict[str, list[str]] = field(default_factory=dict)

    def entry_key(self, model: str | None) -> str | None:
        """Which entry of the file serves this model: its own, `*`, or neither."""
        if (model or "") in self.models:
            return model or ""
        if ANY_MODEL in self.models:
            return ANY_MODEL
        return None

    def steps_for(self, model: str | None) -> list[Step]:
        """The steps for a model id, falling back to `*`, or none at all.

        A model in neither place is not primed. That is the whole scoping rule: the
        file says who it applies to, so there is no second setting to disagree with.
        """
        key = self.entry_key(model)
        return list(self.models[key]) if key is not None else []

    def problems_for(self, model: str | None) -> list[str]:
        """Everything wrong with the part of the file that serves this model.

        Scoped, so a broken entry for one model does not stop another from running.
        Problems with the file itself apply to everyone: when it cannot be parsed at
        all, there is no telling which models it was meant to cover.
        """
        found = [f"{FILENAME}: {problem}" for problem in self.problems.get("", [])]
        key = self.entry_key(model)
        if key is not None:
            found += [f"{key}: {problem}" for problem in self.problems.get(key, [])]
        return found

    @property
    def is_usable(self) -> bool:
        return not self.problems

    @property
    def fails_closed(self) -> bool:
        return self.on_failure != CONTINUE

    @classmethod
    def from_json(cls, data: Any) -> Script:
        if not isinstance(data, dict):
            raise ValueError("priming config must be a JSON object")

        models: dict[str, list[Step]] = {}
        problems: dict[str, list[str]] = {}

        def note(where: str, problem: str) -> None:
            problems.setdefault(where, []).append(problem)

        unknown = [key for key in data if key not in SCRIPT_KEYS]
        if unknown:
            note("", unknown_key_problem(unknown, SCRIPT_KEYS))

        raw_models = data.get("models")
        if raw_models is not None and not isinstance(raw_models, dict):
            note("", "`models` must be an object mapping model ids to lists of steps")
        elif isinstance(raw_models, dict):
            for name, raw_steps in raw_models.items():
                if not isinstance(name, str):
                    note("", f"model id {name!r} is not a string")
                    continue
                # Recorded even when empty, so a broken entry still resolves here
                # rather than silently falling through to the `*` one.
                models[name] = []
                if not isinstance(raw_steps, list):
                    note(name, "must be a list of steps")
                    continue
                for index, raw in enumerate(raw_steps, start=1):
                    step, problem = parse_step(raw)
                    if step is None:
                        note(name, f"step {index} {problem}")
                    else:
                        models[name].append(step)

        attempts = data.get("attempts")
        on_failure = data.get("on_failure")
        return cls(
            models=models,
            attempts=attempts if isinstance(attempts, int) and attempts > 0 else 3,
            on_failure=on_failure if on_failure in (FAIL, CONTINUE) else FAIL,
            problems=problems,
        )


def config_path() -> Path:
    return get_settings().config_dir / FILENAME


_cache: Script | None = None
_cache_key: tuple[Path, float] | None = None


def load() -> Script:
    """Read the script, or an empty one when there is no file.

    Absent is the normal state and means "prime nothing". A file that exists but
    cannot be read is different: it was written to be used, so the failure is carried
    as a problem rather than swallowed into an empty script that primes nothing and
    says nothing. Re-reads when the file changes, so an edit takes effect on the next
    conversation without a restart.
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
        log.warning("Unreadable priming config at %s: %s", path, exc)
        script = Script(problems={"": [f"cannot be read ({exc})"]})
    else:
        log.info(
            "Loaded priming script: %d model(s), %d attempts, on_failure=%s%s",
            len(script.models),
            script.attempts,
            script.on_failure,
            f", {sum(len(p) for p in script.problems.values())} problem(s)"
            if script.problems
            else "",
        )
        for where, found in script.problems.items():
            for problem in found:
                log.warning("Priming config: %s%s", f"{where}: " if where else "", problem)

    _cache, _cache_key = script, key
    return _cache


def describe_problems(problems: Sequence[str]) -> str:
    """The message a user acts on: what is wrong, where the file is, how to opt out."""
    listed = "\n".join(f"  - {problem}" for problem in problems)
    return (
        f"The priming script cannot be used as written:\n{listed}\n"
        f"Fix {config_path()}, or set M365_PRIMING=0 to run without priming."
    )


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
