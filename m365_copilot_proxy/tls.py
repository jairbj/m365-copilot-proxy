"""Where this proxy's TLS trust comes from.

Python does not use the operating system's certificate store by default: every
HTTP library here falls back to `certifi`, a fixed bundle of public CAs. On a
corporate network that performs TLS inspection — a proxy terminating the
connection and re-signing it with a company root — that bundle knows nothing about
the company root, and every request fails with "unable to get local issuer
certificate" even though the certificate is perfectly valid for that machine.

Four independent clients need to agree on this: MSAL (via `requests`), the BizChat
WebSocket (via `websockets`), the image fetch (via `httpx`) and Chromium (which
uses its own NSS store and cannot be pointed at a PEM at all). This module is the
single place that decides, so they cannot drift apart.
"""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path

log = logging.getLogger(__name__)

#: Standard variables anyone working behind a corporate proxy has likely exported
#: already. Consulted after our own `M365_CA_BUNDLE` setting.
CA_BUNDLE_ENV_VARS = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")

_truststore_active = False
_configured = False


def ca_bundle() -> str | None:
    """Path to an explicit CA bundle, or None to use the system/default trust."""
    from m365_copilot_proxy.config import get_settings

    candidates = [("M365_CA_BUNDLE", get_settings().ca_bundle)]
    candidates += [(name, os.environ.get(name, "")) for name in CA_BUNDLE_ENV_VARS]

    for name, value in candidates:
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_file():
            return str(path)
        log.warning("%s points at %s, which is not a file — ignoring it", name, value)
    return None


def configure() -> None:
    """Make the `ssl` module use the OS trust store, unless a bundle was given.

    `truststore` reroutes certificate verification through the platform's native
    store — the Windows/macOS one, or OpenSSL's directory on Linux — which is where
    a corporate root actually lives. It patches `ssl.SSLContext`, so `requests`,
    `httpx` and `websockets` all pick it up at once.

    Idempotent, and never fatal: if it cannot be installed we simply stay on
    certifi, and the user can still set `M365_CA_BUNDLE` by hand.
    """
    global _truststore_active, _configured
    if _configured:
        return
    _configured = True

    if ca_bundle():
        log.debug("Explicit CA bundle configured — leaving the ssl module alone")
        return
    try:
        import truststore

        truststore.inject_into_ssl()
        _truststore_active = True
        log.debug("Verifying certificates against the system trust store")
    except Exception as exc:
        log.debug("Could not use the system trust store (%s) — falling back to certifi", exc)


def truststore_active() -> bool:
    return _truststore_active


def ssl_context() -> ssl.SSLContext | None:
    """SSL context for the WebSocket, or None to let `websockets` build its own."""
    bundle = ca_bundle()
    if not bundle:
        return None
    return ssl.create_default_context(cafile=bundle)


def websocket_ssl_kwargs(url: str) -> dict[str, ssl.SSLContext]:
    """The `ssl` keyword for `websockets.connect` — or nothing at all.

    `websockets` is asymmetric about this: it refuses a non-None `ssl` argument on a
    `ws://` URI, and it equally refuses an explicit `ssl=None` on `wss://`. Omitting
    the key is the only form that satisfies both. On `wss://` it then defaults to
    `ssl=True`, and asyncio builds a context with `ssl.create_default_context()` —
    which, with the system trust store installed by `configure()`, is exactly the
    trust we want anyway.
    """
    if not url.startswith("wss://"):
        return {}
    context = ssl_context()
    return {"ssl": context} if context is not None else {}


def httpx_verify() -> str | bool:
    """The `verify` argument for an httpx client."""
    return ca_bundle() or True


def _walk_causes(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_ssl_error(exc: BaseException) -> bool:
    """True when a certificate problem is anywhere in the exception chain.

    Libraries wrap TLS failures several layers deep — a `requests.SSLError` around a
    urllib3 `MaxRetryError` around the original `SSLCertVerificationError` — so the
    chain has to be walked rather than just type-checking the outermost exception.
    """
    for error in _walk_causes(exc):
        if isinstance(error, ssl.SSLError):
            return True
        name = type(error).__name__
        if name in {"SSLError", "SSLCertVerificationError", "CertificateError"}:
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(error):
            return True
    return False


def explain_ssl_error(
    exc: BaseException, host: str = "", *, suggest_doctor: bool = True
) -> str | None:
    """An actionable message for a TLS failure, or None if this isn't one.

    Returning None for anything else matters: this must never dress up an unrelated
    failure as a certificate problem and send the user chasing the wrong fix.
    """
    if not is_ssl_error(exc):
        return None

    where = f" while connecting to {host}" if host else ""
    trust = ca_bundle() or (
        "the system trust store" if _truststore_active else "the bundled certifi CAs"
    )
    advice = (
        "Run `m365-copilot-proxy doctor` to diagnose, and see the "
        if suggest_doctor
        else "See the "
    )
    return (
        f"TLS certificate verification failed{where}, using {trust}.\n"
        "This usually means corporate TLS inspection: the certificate is signed by "
        "your company's root CA, which Python does not trust yet.\n"
        f"{advice}'Corporate TLS interception' section of the README to point "
        "M365_CA_BUNDLE at your company root."
    )
