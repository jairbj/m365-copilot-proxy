"""Command line interface."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

import typer

from m365_copilot_proxy import tls
from m365_copilot_proxy.config import get_settings

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
        True, help="Keep tones from a previous capture that this run did not see."
    ),
) -> None:
    """Learn this tenant's models and surface by watching the real Copilot UI."""
    from m365_copilot_proxy.bizchat import profile as tenant_profile
    from m365_copilot_proxy.capture import ProfileCollector
    from m365_copilot_proxy.capture import run as run_capture

    _setup_logging()
    collector = ProfileCollector()
    if merge:
        previous = tenant_profile.load()
        collector.tones.update(previous.tones)
        collector.query.update(previous.query)

    try:
        asyncio.run(run_capture(collector))
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
    if captured.query:
        surface = {
            key: captured.query[key]
            for key in ("agent", "scenario", "licenseType")
            if key in captured.query
        }
        typer.echo(f"Surface: {surface}")
    typer.echo(f"Models ({len(captured.tones)}):")
    for model_id, tone in sorted(captured.tones.items()):
        typer.echo(f"  {model_id:<28} tone={tone}")
    typer.echo("\nRename the ids in that file if you prefer different model names.")


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
