"""Learn this tenant's wire profile by watching the real Copilot web client.

The built-in constants in `bizchat/protocol.py` describe one tenant at one moment.
Rather than guess how yours differs — and a wrong `tone` fails the turn outright —
this opens the actual chat in a browser and records what it sends: the Chathub URL's
query fields (which encode the licence surface) and the `type:4` chat invocations
(which carry the `tone` behind each entry in the model picker).

The user drives it: pick a model, send any message, repeat. Every observation is
additive, so capture can be re-run later to pick up a newly offered model.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from m365_copilot_proxy.bizchat import frames, protocol
from m365_copilot_proxy.bizchat.profile import (
    CAPTURED_QUERY_KEYS,
    TenantProfile,
    slug_for_tone,
)
from m365_copilot_proxy.config import get_settings

log = logging.getLogger(__name__)

CHAT_URL = "https://m365.cloud.microsoft/chat"


class CaptureError(RuntimeError):
    """The capture session could not be started."""


def _notice(message: str) -> None:
    print(f"[capture] {message}", file=sys.stderr, flush=True)


@dataclass
class ProfileCollector:
    """Accumulates observations. Owned by the caller so a Ctrl-C still saves."""

    query: dict[str, str] = field(default_factory=dict)
    tones: dict[str, str] = field(default_factory=dict)
    option_sets: list[str] = field(default_factory=list)
    allowed_message_types: list[str] = field(default_factory=list)
    #: Per-socket leftovers, since a frame can straddle two WebSocket messages.
    _buffers: dict[int, str] = field(default_factory=dict, repr=False)

    @property
    def observations(self) -> int:
        return len(self.tones)

    def note_url(self, url: str) -> bool:
        """Record the query fields of a Chathub URL. Returns True if it was one.

        `access_token` is never read: it is a credential, it is per-connection, and
        it has no business being written to a config file.
        """
        parsed = urlparse(url)
        if protocol.WS_PATH.lower() not in parsed.path.lower():
            return False
        params = parse_qs(parsed.query)
        for key in CAPTURED_QUERY_KEYS:
            values = params.get(key)
            if values and values[0]:
                self.query[key] = values[0]
        return True

    def note_frame_payload(self, socket_id: int, payload: str) -> list[str]:
        """Feed one raw WebSocket payload; returns the tones newly discovered."""
        buffer = self._buffers.get(socket_id, "") + payload
        complete, rest = frames.split_frames(buffer)
        self._buffers[socket_id] = rest

        discovered: list[str] = []
        for chunk in complete:
            frame = frames.parse(chunk)
            if not frame or frame.get("type") != frames.TYPE_CLIENT_INVOCATION:
                continue
            if frame.get("target") != "chat":
                continue
            arguments = frame.get("arguments")
            if not isinstance(arguments, list) or not arguments:
                continue
            argument = arguments[0]
            if not isinstance(argument, dict):
                continue

            for key, target in (
                ("optionsSets", self.option_sets),
                ("allowedMessageTypes", self.allowed_message_types),
            ):
                values = argument.get(key)
                if isinstance(values, list):
                    target[:] = [v for v in values if isinstance(v, str)]

            tone = argument.get("tone")
            if isinstance(tone, str) and tone:
                model_id = slug_for_tone(tone)
                if self.tones.get(model_id) != tone:
                    self.tones[model_id] = tone
                    discovered.append(tone)
        return discovered

    def build(self) -> TenantProfile:
        return TenantProfile(
            query=dict(self.query),
            tones=dict(self.tones),
            option_sets=list(self.option_sets),
            allowed_message_types=list(self.allowed_message_types),
        )


async def run(collector: ProfileCollector) -> None:
    """Open the real chat and watch it until the window closes or Ctrl-C."""
    from playwright.async_api import async_playwright

    from m365_copilot_proxy.auth.login import browser_launch_kwargs

    settings = get_settings()
    settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    finished = asyncio.Event()

    def on_websocket(ws: Any) -> None:
        if not collector.note_url(ws.url):
            return
        _notice("Chathub connection seen — tenant surface recorded.")
        socket_id = id(ws)

        def on_frame_sent(payload: Any) -> None:
            if not isinstance(payload, str):
                return  # binary frames are not part of this protocol
            for tone in collector.note_frame_payload(socket_id, payload):
                _notice(f"tone captured: {tone}  ->  model id `{slug_for_tone(tone)}`")

        ws.on("framesent", on_frame_sent)

    async with async_playwright() as pw:
        try:
            context = await pw.chromium.launch_persistent_context(
                str(settings.browser_profile_dir), **browser_launch_kwargs()
            )
        except Exception as exc:
            raise CaptureError(
                f"Could not launch Chromium ({exc}). "
                "Install the browser with `playwright install chromium`, "
                "or point M365_CHROMIUM_PATH at an existing one."
            ) from exc

        try:
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("websocket", on_websocket)
            page.on("close", lambda _: finished.set())
            context.on("close", lambda _: finished.set())

            _notice("Opening Microsoft 365 Copilot.")
            _notice("For each model you want to use: pick it in the model selector,")
            _notice("send any short message, and wait for the reply to start.")
            _notice("Close the window (or press Ctrl-C) when you are done.")
            await page.goto(CHAT_URL, wait_until="domcontentloaded")

            await finished.wait()
        finally:
            try:
                await context.close()
            except Exception as exc:
                log.debug("Browser already closed: %s", exc)
