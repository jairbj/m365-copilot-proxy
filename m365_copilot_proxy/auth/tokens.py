"""Token acquisition, JWT inspection and log redaction."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from m365_copilot_proxy import tls
from m365_copilot_proxy.auth.constants import CHAT_SCOPES, IMAGE_SCOPES
from m365_copilot_proxy.auth.msal_client import cached_account, get_app, save_cache

log = logging.getLogger(__name__)

_ACCESS_TOKEN_RE = re.compile(r"(access_token=)[^&\s]+", re.IGNORECASE)

#: One lock per scope set: concurrent requests share a single refresh instead of
#: racing several against the same account.
_locks: dict[str, asyncio.Lock] = {}


class TlsTrustError(RuntimeError):
    """Certificate verification failed — no amount of re-authenticating fixes it."""


class NeedsLoginError(RuntimeError):
    """No usable token and no way to get one without a human at a browser."""

    def __init__(self, detail: str = "") -> None:
        message = "Not signed in. Run `m365-copilot-proxy login` to authenticate."
        super().__init__(f"{message} ({detail})" if detail else message)


@dataclass(frozen=True)
class TokenClaims:
    """The claims we care about from our own access token."""

    oid: str
    tid: str
    exp: int
    upn: str | None = None
    audience: str | None = None

    @property
    def chathub_path(self) -> str:
        """The account-specific Chathub path segment: `<user-oid>@<tenant-id>`."""
        return f"{self.oid}@{self.tid}"

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(self.exp, tz=UTC)

    @property
    def seconds_remaining(self) -> float:
        return self.exp - datetime.now(tz=UTC).timestamp()


def redact(text: str) -> str:
    """Strip `access_token=...` from a string so it is safe to log.

    The Sydney token rides in the WebSocket query string per the protocol, so every
    log line that touches a WS URL must go through this.
    """
    return _ACCESS_TOKEN_RE.sub(r"\1REDACTED", text)


def decode_jwt(token: str) -> TokenClaims:
    """Read the claims out of our own access token.

    Signature is deliberately not verified: this is a token we just received from
    Entra ID for ourselves, and we only need `oid`/`tid` to build the Chathub path
    plus `exp` to report freshness. Nothing here is a security decision.
    """
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("Access token is not a JWT")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    try:
        return TokenClaims(
            oid=claims["oid"],
            tid=claims["tid"],
            exp=int(claims["exp"]),
            upn=claims.get("upn") or claims.get("preferred_username"),
            audience=claims.get("aud"),
        )
    except KeyError as exc:
        raise ValueError(f"Access token is missing the {exc} claim") from exc


def _acquire_silent(scopes: list[str]) -> str:
    """Blocking MSAL silent acquisition. Called in a worker thread."""
    account = cached_account()
    if account is None:
        raise NeedsLoginError("no account in the token cache")
    try:
        result = get_app().acquire_token_silent(scopes, account=account)
    except Exception as exc:
        # A TLS failure here is not "you need to log in again" — logging in will
        # fail the same way. Report the real cause.
        explanation = tls.explain_ssl_error(exc, "login.microsoftonline.com")
        if explanation:
            raise TlsTrustError(explanation) from exc
        raise
    save_cache()
    if not result or "access_token" not in result:
        detail = (result or {}).get("error_description") or "silent refresh failed"
        raise NeedsLoginError(detail.splitlines()[0])
    return result["access_token"]


async def get_token(scopes: list[str]) -> str:
    """Acquire an access token for `scopes`, refreshing silently when needed.

    Raises `NeedsLoginError` when only an interactive login can fix it — the server
    turns that into a 401 telling the user to run the `login` command.
    """
    key = " ".join(sorted(scopes))
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(_acquire_silent, scopes)


async def get_chat_token() -> str:
    """Token for the BizChat WebSocket (audience `.../sydney`)."""
    return await get_token(CHAT_SCOPES)


async def get_image_token() -> str:
    """Token that authorizes fetching generated-image artifact URLs."""
    return await get_token(IMAGE_SCOPES)


def account_summary() -> dict | None:
    """Account details for the `status` command, or None when not signed in."""
    account = cached_account()
    if account is None:
        return None
    return {
        "username": account.get("username"),
        "home_account_id": account.get("home_account_id"),
        "environment": account.get("environment"),
    }
