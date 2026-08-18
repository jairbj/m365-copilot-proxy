"""Runtime configuration.

Everything is overridable through `M365_*` environment variables (or a `.env` in
the working directory). Defaults are tuned for the single-user, run-it-on-your-own
-machine case this proxy is built for: bind to loopback, keep state under the
user's config dir, and never enable the debugging side-channels by default.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_config_dir() -> Path:
    """`$XDG_CONFIG_HOME/m365-copilot-proxy`, falling back to `~/.config/...`."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "m365-copilot-proxy"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="M365_", env_file=".env", extra="ignore")

    # --- storage ---
    config_dir: Path = Field(default_factory=default_config_dir)

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8765

    # --- login (Playwright) ---
    #: Seconds to wait for the human to finish SSO/MFA in the opened window.
    login_timeout: float = 600.0
    #: A coherent Linux Chrome UA. Deliberately NOT spoofing another OS: a Windows
    #: UA on a Linux `navigator.platform` is itself a flaggable fingerprint.
    login_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    login_locale: str = "en-US"
    login_timezone: str = "UTC"
    #: Explicit Chromium binary. Empty = let Playwright use its bundled browser.
    chromium_path: str = ""
    #: Let the browser accept certificates it cannot verify. Chromium reads its own
    #: NSS store and cannot be pointed at a PEM, so behind corporate TLS inspection
    #: the choice is importing the company root into NSS (see the README) or this.
    #: It applies only to the windows this tool opens, which only visit Microsoft.
    browser_ignore_tls_errors: bool = False

    # --- TLS ---
    #: Path to a PEM bundle to verify certificates against. Needed on networks that
    #: perform TLS inspection. Left empty, the system trust store is used.
    #: `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` are honoured as fallbacks.
    ca_bundle: str = ""

    # --- BizChat behaviour ---
    #: M365 soft-caps output around ~3k tokens and CONCLUDES EARLY rather than
    #: truncating, so a too-long answer looks clean but is incomplete. Flag it with
    #: finish_reason="length" so harnesses ask for a continuation. 0 disables.
    output_char_ceiling: int = 12_000
    #: Seconds without any frame from the server before we give up on a turn.
    turn_timeout: float = 300.0
    #: Evict a pooled conversation after this many seconds of inactivity.
    session_idle_timeout: float = 1800.0
    #: Rotate the ConversationId this many turns before the server-side cap (600).
    conversation_turn_headroom: int = 10
    #: Enable image generation on every turn instead of only for the image model id.
    images_always: bool = False
    #: Ground answers in the tenant's work content (the web client's "Work IQ"
    #: toggle) when the model id does not say. Off by default: it costs latency,
    #: and company content should not enter a conversation unasked. Turn it on per
    #: request with the `-work` model suffix.
    work_iq: bool = False
    #: Append NDJSON of every SignalR frame to `<config_dir>/frames/<id>.ndjson`.
    dump_frames: bool = False
    log_level: str = "INFO"

    @property
    def msal_cache_file(self) -> Path:
        return self.config_dir / "msal-cache.json"

    @property
    def browser_profile_dir(self) -> Path:
        return self.config_dir / "browser-profile"

    @property
    def frames_dir(self) -> Path:
        return self.config_dir / "frames"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
