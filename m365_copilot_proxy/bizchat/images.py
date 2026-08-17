"""Server-side image generation artifacts.

BizChat returns generated images as URLs on `designerapp.officeapps.live.com`,
behind SharePoint Embedded. Those URLs 401 for the Sydney token — the artifact
wants a token for the designerapp service — and they are useless to an OpenAI
client anyway, so we fetch the bytes ourselves and hand back a self-contained
data URI.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: Above this, a data URI is more of a burden than a convenience for the client.
MAX_INLINE_BYTES = 8 * 1024 * 1024


@dataclass
class GeneratedImage:
    reference_urls: list[str] = field(default_factory=list)
    file_token: str | None = None
    poll_url: str | None = None
    size: str | None = None
    orientation: str | None = None
    #: Server status; 2 means ready.
    status: int | None = None

    @property
    def url(self) -> str | None:
        return self.reference_urls[0] if self.reference_urls else None


def capture_images(message: dict[str, Any], into: dict[str, GeneratedImage]) -> None:
    """Collect images off a bot message into `into`, keyed by artifact token.

    The same artifact arrives repeatedly as a progress snapshot with a climbing
    `status`, and once more in the final stream item, so entries are upserted and
    the readiest copy wins.
    """
    entries = message.get("contentGenerationProgressList")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        urls = entry.get("ImageReferenceUrls")
        if not isinstance(urls, list) or not urls:
            continue
        key = entry.get("fileToken") or urls[0]
        previous = into.get(key)
        status = entry.get("status")
        if previous is not None and (previous.status or 0) >= (status or 0):
            continue
        into[key] = GeneratedImage(
            reference_urls=[u for u in urls if isinstance(u, str)],
            file_token=entry.get("fileToken"),
            poll_url=entry.get("pollUrl"),
            size=entry.get("size"),
            orientation=entry.get("orientation"),
            status=status if isinstance(status, int) else None,
        )


async def fetch_image_bytes(url: str, artifact_token: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {artifact_token}"})
        response.raise_for_status()
        content_type = response.headers.get("content-type", "image/png").split(";")[0]
        return response.content, content_type


async def render_images_markdown(images: list[GeneratedImage]) -> str:
    """Render captured images as markdown any OpenAI client can display.

    Falls back to the bare (unauthenticated, hence broken) URL rather than dropping
    the image silently, so a failure is visible instead of looking like an empty
    answer.
    """
    if not images:
        return ""

    from m365_copilot_proxy.auth.tokens import get_image_token

    token: str | None = None
    try:
        token = await get_image_token()
    except Exception as exc:
        log.info("Could not acquire the image artifact token: %s", exc)

    parts: list[str] = []
    for image in images:
        url = image.url
        if not url:
            continue
        if token:
            try:
                data, content_type = await fetch_image_bytes(url, token)
                if len(data) <= MAX_INLINE_BYTES:
                    encoded = base64.b64encode(data).decode("ascii")
                    parts.append(f"![generated image](data:{content_type};base64,{encoded})")
                    continue
                log.info("Generated image is %d bytes — linking instead of inlining", len(data))
            except Exception as exc:
                log.info("Could not fetch the generated image: %s", exc)
        parts.append(f"![generated image]({url})")
    return "\n\n".join(parts)
