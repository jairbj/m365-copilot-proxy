"""Command line interface."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from m365_copilot_proxy import tls
from m365_copilot_proxy.config import get_settings
from m365_copilot_proxy.openai_api.tools import TOOL_CONTRACT

if TYPE_CHECKING:
    from m365_copilot_proxy.agent_instructions import Document

#: Where pi keeps its custom providers.
PI_MODELS_PATH = Path.home() / ".pi" / "agent" / "models.json"

#: What `priming --init` writes: the message that was found to work by hand, with the
#: tool list as a placeholder so it stays current instead of going stale in a file.
STARTER_PRIMING = {
    "attempts": 3,
    "on_failure": "fail",
    "models": {
        "agent:agent-1": [
            {
                "label": "use the tools",
                "text": (
                    "Quando for ler ou gravar arquivos ou executar comandos, sempre use "
                    "as ferramentas do seu prompt de agente ou as que eu te passar.\n\n"
                    "{{tools_prompt}}\n\n"
                    'Se você entendeu, responda apenas "agente-ok".'
                ),
                "expect": "agente-ok",
            }
        ]
    },
}

cli = typer.Typer(
    add_completion=False,
    help="OpenAI-compatible proxy for Microsoft 365 Copilot (BizChat).",
)


def _setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=(level or get_settings().log_level).upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@cli.command()
def login() -> None:
    """Sign in to Microsoft 365 in a browser window (one time)."""
    from m365_copilot_proxy.auth.login import LoginError
    from m365_copilot_proxy.auth.login import login as do_login
    from m365_copilot_proxy.auth.tokens import decode_jwt

    _setup_logging()
    try:
        token = asyncio.run(do_login())
    except LoginError as exc:
        typer.secho(f"Login failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    claims = decode_jwt(token)
    typer.echo(f"Signed in as {claims.upn or claims.oid}")
    typer.echo(f"Token audience: {claims.audience}")
    typer.echo(f"Token expires at: {claims.expires_at.isoformat()}")


@cli.command()
def status() -> None:
    """Show the signed-in account and refresh a token silently."""
    from m365_copilot_proxy.auth.tokens import (
        NeedsLoginError,
        account_summary,
        decode_jwt,
        get_chat_token,
    )

    _setup_logging("WARNING")
    account = account_summary()
    if account is None:
        typer.secho("Not signed in. Run `m365-copilot-proxy login`.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    typer.echo(f"Account: {account.get('username')}")
    try:
        claims = decode_jwt(asyncio.run(get_chat_token()))
    except NeedsLoginError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Tenant:  {claims.tid}")
    typer.echo(f"Audience: {claims.audience}")
    typer.echo(
        f"Token valid for {int(claims.seconds_remaining // 60)} min "
        f"(until {claims.expires_at.isoformat()})"
    )
    typer.echo("Silent refresh works — no browser needed.")


@cli.command()
def doctor() -> None:
    """Check whether this machine can reach Microsoft over TLS."""
    from m365_copilot_proxy import diagnostics

    _setup_logging("WARNING")
    for line in diagnostics.environment_report():
        typer.echo(line)
    typer.echo("")

    failed = False
    for result in diagnostics.run_checks():
        if result.ok:
            typer.secho(f"PASS  {result.name}", fg=typer.colors.GREEN)
            typer.echo(f"      {result.detail}")
        else:
            failed = True
            typer.secho(f"FAIL  {result.name}", fg=typer.colors.RED)
            for line in result.detail.splitlines():
                typer.echo(f"      {line}")

    if failed:
        typer.echo("")
        typer.secho(
            "See the 'Corporate TLS interception' section of the README.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    typer.echo("")
    typer.echo("TLS looks good.")


@cli.command()
def capture(
    merge: bool = typer.Option(
        True, help="Keep tones, surfaces and agents from previous captures this run did not see."
    ),
    record_api: bool = typer.Option(
        False,
        help="Also record the site's own write calls (no headers) for later study.",
    ),
) -> None:
    """Learn this tenant's models and surfaces by watching the real Copilot UI."""
    from m365_copilot_proxy.bizchat import profile as tenant_profile
    from m365_copilot_proxy.bizchat import protocol
    from m365_copilot_proxy.capture import ProfileCollector
    from m365_copilot_proxy.capture import run as run_capture

    _setup_logging()
    collector = ProfileCollector()
    if merge:
        # Each run records the surface the Work IQ toggle is currently in, so
        # merging is what lets two runs build one complete profile.
        previous = tenant_profile.load()
        collector.tones.update(previous.tones)
        collector.surfaces.update(previous.surfaces)
        # Agents are matched by their id, not their name, so a renamed one is
        # updated in place rather than reappearing under a fresh `agent-N`.
        collector.agents.update(previous.agents)

    recorder = None
    try:
        recorder = asyncio.run(run_capture(collector, record_api=record_api))
    except KeyboardInterrupt:
        typer.echo("")  # keep the ^C off the summary line
    except Exception as exc:
        typer.secho(f"Capture failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    captured = collector.build()
    if captured.is_empty:
        typer.secho(
            "Nothing captured. Was a message sent in the chat window?",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1)

    path = tenant_profile.save(captured)
    typer.echo(f"\nSaved {path}")

    for name, surface in sorted(captured.surfaces.items()):
        summary = {
            key: surface.query[key]
            for key in ("agent", "scenario", "licenseType")
            if key in surface.query
        }
        typer.echo(f"Surface {name}: {summary} ({len(surface.option_sets)} optionsSets)")

    missing = {tenant_profile.WORK, tenant_profile.WEB} - set(captured.surfaces)
    for name in sorted(missing):
        typer.secho(
            f"No '{name}' surface yet — run capture again with Work IQ "
            f"{'on' if name == tenant_profile.WORK else 'off'} to record it.",
            fg=typer.colors.YELLOW,
        )

    typer.echo(f"Models ({len(captured.tones)}):")
    for model_id, tone in sorted(captured.tones.items()):
        typer.echo(f"  {model_id:<28} tone={tone}")

    if captured.agents:
        typer.echo(f"Agents ({len(captured.agents)}):")
        for slug in sorted(captured.agents):
            typer.echo(f"  {protocol.AGENT_ID_PREFIX}{slug}")
        typer.echo("An agent brings its own model and grounding — neither is selectable.")
        typer.echo("Paste its instructions with `m365-copilot-proxy prompt`.")

    if recorder is not None and recorder.count:
        typer.echo(f"\nRecorded {recorder.count} write calls to {recorder.path}")
        typer.echo("Nothing replays them — they are there to read.")

    typer.echo("\nRename the ids in that file if you prefer different model names.")
    typer.echo("Add `-work` to any id to ground that turn in your work content.")


@cli.command()
def prompt(
    key: str = typer.Option(None, help="Show one recorded conversation instead of the latest."),
    show_list: bool = typer.Option(False, "--list", help="List what has been recorded."),
    out: str = typer.Option("", "--out", help="Write the document to a file."),
    raw: bool = typer.Option(False, help="The client's system prompt alone."),
    contract: bool = typer.Option(False, help="The tool contract alone."),
    tools: bool = typer.Option(False, help="The list of the client's tools alone."),
) -> None:
    """Show the instructions to paste into a declarative agent.

    M365 Copilot often ignores the system prompt the proxy inlines, and honours a
    declarative agent's instructions instead. This prints what to put there — the
    contract, the client's tools and its system prompt — and a turn bound to an agent
    sends none of it, so whatever is not pasted is not sent. The document goes to
    stdout and its size to stderr, so it pipes straight into a file or a clipboard.
    """
    from m365_copilot_proxy import agent_instructions

    _setup_logging("WARNING")

    if show_list:
        records = agent_instructions.list_records()
        if not records:
            typer.secho("Nothing recorded yet.", fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(1)
        for entry in records:
            typer.echo(
                f"{entry.key}  {len(entry.system_text):>6} prompt  "
                f"{len(entry.tool_text):>6} tools  "
                f"{entry.model or '?':<20} {entry.recorded_at or ''}"
            )
            if entry.label:
                typer.echo(f"    {entry.label}")
        return

    picked = (("--raw", raw), ("--contract", contract), ("--tools", tools))
    chosen = [name for name, on in picked if on]
    if len(chosen) > 1:
        typer.secho(
            f"Choose one of {', '.join(chosen)}, not several.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(2)

    if contract:
        # The contract stands on its own: it is the same text for every client, so
        # it needs no recording to exist.
        document = agent_instructions.compose(contract=True, tools=False, prompt=False)
    else:
        record = agent_instructions.load(key) if key else agent_instructions.latest()
        if record is None:
            typer.secho(
                "Nothing recorded yet. Send one request through the proxy first, "
                "then run this again.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(1)
        only = raw or tools
        document = record.compose(
            contract=not only,
            tools=tools or not only,
            prompt=raw or not only,
        )

    if out:
        destination = Path(out)
        destination.write_text(document.text, encoding="utf-8")
        typer.echo(f"Wrote {destination}")
    else:
        typer.echo(document.text)

    _report_size(document)


def _report_size(document: Document) -> None:
    """Size breakdown on stderr, so stdout stays pasteable."""
    from m365_copilot_proxy.agent_instructions import INSTRUCTIONS_LIMIT

    if document.key:
        typer.secho(f"\nFrom {document.model or '?'} ({document.key})", err=True)
    for title, chars in document.breakdown():
        typer.secho(f"  {title:<16} {chars:>7,} chars", err=True)

    limit = INSTRUCTIONS_LIMIT
    if document.fits:
        typer.secho(
            f"  {'Total':<16} {document.chars:>7,} chars — fits the agent's "
            f"{limit:,}-character field.",
            fg=typer.colors.GREEN,
            err=True,
        )
        return
    typer.secho(
        f"  {'Total':<16} {document.chars:>7,} chars — {document.over_by:,} over the "
        f"agent's {limit:,}-character field. Trim it before pasting; nothing here "
        "cuts it for you.",
        fg=typer.colors.YELLOW,
        err=True,
    )


@cli.command()
def priming(
    model: str = typer.Option("", help="Which model's script to show. Default: the last used."),
    init: bool = typer.Option(False, "--init", help="Write a starter priming.json."),
) -> None:
    """Show the opening exchange a new conversation will be primed with.

    A declarative agent honours its instructions but does not always act on them. The
    script says so in a turn of its own and checks the answer, so a conversation that
    did not take it in is thrown away before any real work is sent to it.
    """
    from m365_copilot_proxy import agent_instructions
    from m365_copilot_proxy import priming as priming_config

    _setup_logging("WARNING")
    path = priming_config.config_path()

    if init:
        if path.exists():
            typer.secho(f"{path} already exists — not overwriting.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(STARTER_PRIMING, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        priming_config.reset_cache()
        typer.echo(f"Wrote {path}")
        typer.echo("Edit the text and the model id, then run `priming` to see it rendered.")
        return

    script = priming_config.load()

    problems = [
        f"{where}: {problem}" if where else problem
        for where, found in script.problems.items()
        for problem in found
    ]
    if problems:
        # No rendering when the file is broken: showing the steps that survived is
        # exactly the illusion that hides a missing one.
        typer.secho(priming_config.describe_problems(problems), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    if not script.models:
        typer.secho(
            f"No priming script at {path}. Run `m365-copilot-proxy priming --init`.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1)

    record = agent_instructions.latest()
    target = model or (record.model if record else "") or priming_config.ANY_MODEL
    steps = script.steps_for(target)
    if not steps:
        typer.secho(
            f"Nothing primes `{target}`. Models in the script: "
            f"{', '.join(sorted(script.models)) or 'none'}.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1)

    # Rendered with the last real request's tools and prompt, so this is what the
    # model would actually receive rather than a template with holes in it.
    rendered = priming_config.rendered_steps(
        steps,
        {
            "tools_prompt": record.tool_text if record else "",
            "system_prompt": record.system_text if record else "",
            "contract": TOOL_CONTRACT,
            "user_message": "<the client's first message>",
        },
    )

    typer.secho(
        f"{target}: {len(rendered)} step(s), {script.attempts} attempt(s), "
        f"on_failure={script.on_failure}",
        err=True,
    )
    if record is None:
        typer.secho(
            "No request recorded yet, so {{tools_prompt}} and {{system_prompt}} are "
            "empty here — they fill in for real at request time.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    for index, step in enumerate(rendered, start=1):
        typer.secho(f"\n--- step {index}/{len(rendered)} ({step.describe()}) ---", err=True)
        typer.echo(step.text)


@cli.command("pi-config")
def pi_config(
    out: str = typer.Option("", "--out", help="Where to write. Default: ~/.pi/agent/models.json."),
    show: bool = typer.Option(False, "--print", help="Print the result instead of writing it."),
) -> None:
    """Write pi's provider config from the models `capture` found.

    Lists this tenant's tones — each in both Work IQ surfaces — plus any declarative
    agent, so `/model` offers what you actually have instead of a snapshot of someone
    else's tenant. Only the `m365` provider is touched; anything else in the file is
    left as it was.
    """
    from m365_copilot_proxy import pi_config as generator

    _setup_logging("WARNING")
    try:
        block = generator.provider()
    except generator.NothingCaptured as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(1) from exc

    path = Path(out) if out else PI_MODELS_PATH
    existing: object = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A hand-edited config is not something to clobber over a typo.
            typer.secho(
                f"{path} exists but could not be read as JSON ({exc}). "
                "Fix or move it; nothing was written.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1) from exc

    document = generator.merge_into(existing, block)
    rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"

    if show:
        typer.echo(rendered)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")

    typer.echo(f"Wrote {path} — {len(block['models'])} models:")
    for model in block["models"]:
        typer.echo(f"  {model['id']:<28} {model['name']}")
    others = [name for name in document["providers"] if name != generator.PROVIDER]
    if others:
        typer.echo(f"Left your other provider(s) alone: {', '.join(sorted(others))}")
    typer.echo("Start the proxy, then `pi --list-models` shows them and `/model` picks one.")


@cli.command()
def logout(
    keep_browser_profile: bool = typer.Option(
        False, help="Keep the browser profile so the next login stays SSO-silent."
    ),
) -> None:
    """Forget the cached tokens (and, by default, the browser profile)."""
    from m365_copilot_proxy.auth.msal_client import forget_account

    _setup_logging("WARNING")
    forget_account()
    typer.echo("Token cache cleared.")
    if not keep_browser_profile:
        profile = get_settings().browser_profile_dir
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
            typer.echo("Browser profile removed.")


@cli.command()
def serve(
    host: str = typer.Option(None, help="Bind address (default 127.0.0.1)."),
    port: int = typer.Option(None, help="Port (default 8765)."),
    log_level: str = typer.Option(None, help="Logging level."),
) -> None:
    """Run the OpenAI-compatible server."""
    from m365_copilot_proxy.auth.tokens import account_summary
    from m365_copilot_proxy.openai_api.server import run

    _setup_logging(log_level)
    settings = get_settings()
    if account_summary() is None:
        typer.secho(
            "Warning: not signed in — requests will fail with 401 until you run "
            "`m365-copilot-proxy login`.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    bind_host = host or settings.host
    bind_port = port or settings.port
    typer.echo(f"Serving the OpenAI API on http://{bind_host}:{bind_port}/v1")
    run(host=bind_host, port=bind_port)


def main() -> None:
    tls.configure()
    try:
        cli()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
