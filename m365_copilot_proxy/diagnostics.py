"""Answer the question "can this machine reach Microsoft over TLS at all?".

Certificate problems on a corporate network are painful to diagnose remotely: the
failure surfaces as a wall of traceback from whichever library happened to make the
first request, and the two endpoints this proxy needs go through *different* TLS
stacks — MSAL uses `requests`, the chat uses a raw asyncio socket — so one can work
while the other does not.
"""

from __future__ import annotations

import socket
import ssl
import sys
from dataclasses import dataclass

from m365_copilot_proxy import tls
from m365_copilot_proxy.auth.constants import AUTHORITY
from m365_copilot_proxy.bizchat import protocol

OPENID_URL = f"{AUTHORITY}/v2.0/.well-known/openid-configuration"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def environment_report() -> list[str]:
    """What this process is actually trusting, in the order it was decided."""
    import certifi

    bundle = tls.ca_bundle()
    lines = [
        f"Python           {sys.version.split()[0]} ({sys.platform})",
        f"certifi bundle   {certifi.where()}",
        f"OpenSSL default  {ssl.get_default_verify_paths().cafile or '(none)'}",
        f"CA bundle in use {bundle or '(none — using the default trust)'}",
        f"System trust     {'active (truststore)' if tls.truststore_active() else 'not in use'}",
    ]
    return lines


def check_https() -> CheckResult:
    """The exact endpoint MSAL hits first, through the same library MSAL uses."""
    import requests

    try:
        response = requests.get(OPENID_URL, timeout=20, verify=tls.ca_bundle() or True)
        response.raise_for_status()
        issuer = response.json().get("issuer", "?")
        return CheckResult("HTTPS to login.microsoftonline.com", True, f"issuer {issuer}")
    except Exception as exc:
        return CheckResult(
            "HTTPS to login.microsoftonline.com",
            False,
            tls.explain_ssl_error(exc, "login.microsoftonline.com", suggest_doctor=False)
            or str(exc),
        )


def check_tls_handshake(host: str = protocol.WS_HOST, port: int = 443) -> CheckResult:
    """A bare TLS handshake to the chat host — the WebSocket's own trust path."""
    name = f"TLS handshake to {host}"
    context = tls.ssl_context() or ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=20) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secure:
                cert = secure.getpeercert() or {}
                issuer = dict(x[0] for x in cert.get("issuer", ())).get(
                    "organizationName", "?"
                )
                return CheckResult(name, True, f"certificate issued by {issuer}")
    except Exception as exc:
        explanation = tls.explain_ssl_error(exc, host, suggest_doctor=False)
        return CheckResult(name, False, explanation or str(exc))


def run_checks() -> list[CheckResult]:
    return [check_https(), check_tls_handshake()]
