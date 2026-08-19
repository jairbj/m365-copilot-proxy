"""Command line interface."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from m365_copilot_proxy import tls
from m365_copilot_proxy.config import get_settings

if TYPE_CHECKING:
    from m365_copilot_proxy.agent_instructions import Document

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
    raw: bool = typer.Option(False, help="The client's system prompt alone, no tool contract."),
    contract: bool = typer.Option(False, help="The tool contract alone, no system prompt."),
) -> None:
    """Show the instructions to paste into a declarative agent.

    M365 Copilot often ignores the system prompt the proxy inlines, and honours a
    declarative agent's instructions instead. This prints what to put there: the
    document goes to stdout, its size to stderr, so it can be piped straight into a
    file or a clipboard command.
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
                f"{entry.key}  {len(entry.system_text):>6} chars  "
                f"{entry.model or '?':<20} {entry.recorded_at or ''}"
            )
            if entry.label:
                typer.echo(f"    {entry.label}")
        return

    if raw and contract:
        typer.secho("Choose --raw or --contract, not both.", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    if contract:
        # The contract stands on its own: it is the same text for every client, so
        # it needs no recording to exist.
        document = agent_instructions.compose(contract=True, prompt=False)
    else:
        record = agent_instructions.load(key) if key else agent_instructions.latest()
        if record is None:
            typer.secho(
                "No system prompt recorded yet. Send one request through the proxy "
                "first, then run this again.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(1)
        document = record.compose(contract=not raw, prompt=True)

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
