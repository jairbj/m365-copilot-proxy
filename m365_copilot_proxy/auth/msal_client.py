"""MSAL public client with an on-disk token cache.

The cache file holds a refresh token, so it is written 0600 and never logged. It
is what makes the browser a one-time cost: after the first interactive login every
later start acquires tokens silently over HTTP.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import msal

from m365_copilot_proxy.auth.constants import AUTHORITY, CLIENT_ID
from m365_copilot_proxy.config import get_settings

log = logging.getLogger(__name__)

_app: msal.PublicClientApplication | None = None
_cache: msal.SerializableTokenCache | None = None


def _load_cache(path: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if path.exists():
        try:
            cache.deserialize(path.read_text(encoding="utf-8"))
        except Exception as exc:  # a corrupt cache must not brick the proxy
            log.warning("Ignoring unreadable token cache at %s: %s", path, exc)
    return cache


def save_cache() -> None:
    """Persist the cache if MSAL touched it. Atomic write, 0600."""
    if _cache is None or not _cache.has_state_changed:
        return
    path = get_settings().msal_cache_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(_cache.serialize(), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def get_app() -> msal.PublicClientApplication:
    """The process-wide MSAL client, with its cache loaded from disk."""
    global _app, _cache
    if _app is None:
        _cache = _load_cache(get_settings().msal_cache_file)
        _app = msal.PublicClientApplication(
            CLIENT_ID, authority=AUTHORITY, token_cache=_cache
        )
    return _app


def reset_app() -> None:
    """Drop the in-process client so the next call reloads the cache from disk."""
    global _app, _cache
    _app = None
    _cache = None


def cached_account() -> dict | None:
    """The signed-in account, or None when no interactive login has happened yet."""
    accounts = get_app().get_accounts()
    return accounts[0] if accounts else None


def forget_account() -> None:
    """Remove the cached account and delete the cache file (the `logout` path)."""
    app = get_app()
    for account in app.get_accounts():
        app.remove_account(account)
    save_cache()
    get_settings().msal_cache_file.unlink(missing_ok=True)
    reset_app()
