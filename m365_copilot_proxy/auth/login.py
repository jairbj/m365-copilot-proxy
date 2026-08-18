"""Interactive sign-in: a real browser window, driven by the human, once.

This is the only place Playwright is used. It opens Microsoft's own authorize
page, waits for the person to complete SSO/MFA however their tenant requires
(password, TOTP, push, FIDO2, a federated IdP — we do not care, we never touch the
form), captures the authorization code and exchanges it with PKCE. After that the
refresh token in the MSAL cache carries the proxy, so no browser stays resident.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from m365_copilot_proxy import tls
from m365_copilot_proxy.auth.constants import CHAT_SCOPES, REDIRECT_URI
from m365_copilot_proxy.auth.msal_client import get_app, save_cache
from m365_copilot_proxy.config import get_settings

log = logging.getLogger(__name__)


class LoginError(RuntimeError):
    pass


def _describe(exc: Exception) -> str:
    """The TLS explanation when that is the cause, otherwise the plain error."""
    return tls.explain_ssl_error(exc, "login.microsoftonline.com") or str(exc)


def _notice(message: str) -> None:
    """Blocking instructions to a human: must show even with logging off."""
    print(f"[login] {message}", file=sys.stderr, flush=True)


def browser_launch_kwargs() -> dict[str, Any]:
    """Launch options shared by every browser window this proxy opens.

    The `capture` command reuses these so it presents the same device fingerprint
    as the login did — a profile that Entra ID already knows.
    """
    settings = get_settings()
    kwargs: dict[str, Any] = {
        "headless": False,  # the entire point: a human has to see and drive this
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            # `navigator.webdriver` and the AutomationControlled blink feature are
            # the loudest automation tells Entra ID's risk engine reads.
            "--disable-blink-features=AutomationControlled",
        ],
        "user_agent": settings.login_user_agent,
        "locale": settings.login_locale,
        "timezone_id": settings.login_timezone,
        "viewport": {"width": 1280, "height": 900},
    }
    if settings.chromium_path:
        kwargs["executable_path"] = settings.chromium_path
    if settings.browser_ignore_tls_errors:
        kwargs["ignore_https_errors"] = True
    return kwargs


async def _capture_auth_response(auth_uri: str, timeout: float) -> dict[str, str]:
    """Open the authorize URL and return the `code`/`state` from the redirect.

    The `nativeclient` redirect URI is meant for embedded native hosts to
    intercept; a real browser follows it one hop further to `/common/wrongplace`,
    so the `?code=` exists only transiently. We read it off the navigation request
    instead of waiting for the URL to settle.
    """
    from playwright.async_api import async_playwright

    settings = get_settings()
    settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    captured: asyncio.Future[dict[str, str]] = loop.create_future()

    def on_request(request: Any) -> None:
        url = request.url
        if "/oauth2/nativeclient" not in url or "code=" not in url:
            return
        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(url).query)
        code = params.get("code", [""])[0]
        if code and not captured.done():
            log.info("Captured authorization code from the nativeclient redirect")
            captured.set_result({"code": code, "state": params.get("state", [""])[0]})

    async with async_playwright() as pw:
        try:
            context = await pw.chromium.launch_persistent_context(
                # A persistent profile keeps the Entra session/device cookies, so
                # later logins are SSO-silent and look like a returning familiar
                # device rather than a brand-new one every time.
                str(settings.browser_profile_dir),
                **browser_launch_kwargs(),
            )
        except Exception as exc:
            raise LoginError(
                f"Could not launch Chromium ({exc}). "
                "Install the browser with `playwright install chromium`, "
                "or point M365_CHROMIUM_PATH at an existing one."
            ) from exc

        try:
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("request", on_request)

            _notice("A browser window has opened — complete the Microsoft sign-in there.")
            _notice(f"Waiting up to {int(timeout)}s.")
            try:
                await page.goto(auth_uri, wait_until="domcontentloaded")
            except Exception as exc:
                # A fast SSO round-trip can abort the navigation out from under us;
                # the code still arrives on the request listener.
                log.debug("Initial navigation ended early: %s", exc)

            try:
                return await asyncio.wait_for(asyncio.shield(captured), timeout=timeout)
            except TimeoutError as exc:
                raise LoginError(
                    f"Timed out after {int(timeout)}s waiting for the sign-in to complete."
                ) from exc
        finally:
            await context.close()


async def login(scopes: list[str] | None = None) -> str:
    """Run the interactive login and return the acquired access token."""
    scopes = scopes or CHAT_SCOPES
    app = get_app()
    try:
        flow = app.initiate_auth_code_flow(scopes, redirect_uri=REDIRECT_URI)
    except Exception as exc:
        # This is the first call that touches the network, so it is where a
        # corporate TLS-inspecting proxy shows up.
        raise LoginError(_describe(exc)) from exc
    if "auth_uri" not in flow:
        raise LoginError(f"Could not build the authorization URL: {flow}")

    auth_response = await _capture_auth_response(
        flow["auth_uri"], get_settings().login_timeout
    )

    try:
        result = await asyncio.to_thread(app.acquire_token_by_auth_code_flow, flow, auth_response)
    except Exception as exc:
        raise LoginError(_describe(exc)) from exc
    save_cache()
    if "access_token" not in result:
        detail = result.get("error_description") or result.get("error") or str(result)
        raise LoginError(f"Token exchange failed: {detail.splitlines()[0]}")

    account = (result.get("id_token_claims") or {}).get("preferred_username")
    _notice(f"Signed in{f' as {account}' if account else ''}.")
    return result["access_token"]
