"""Writing pi's provider config from what `capture` learned.

`pi` reads custom providers from `~/.pi/agent/models.json`, and the entries it needs
are exactly what the tenant profile already holds: the tones the model picker offered,
each in both Work IQ surfaces, plus any declarative agent. Hand-writing that is a
transcription exercise with a wrong answer — an id the proxy does not serve falls back
to the default tone and quietly gives you a different model than the one you picked.

So this builds the block, and the caller merges it into whatever else lives in that
file. Nothing here reads or writes the file itself: that keeps the shape testable, and
keeps the one destructive step in the command where the user can see it.
"""

from __future__ import annotations

from typing import Any

from m365_copilot_proxy.bizchat import profile as tenant_profile
from m365_copilot_proxy.bizchat import protocol
from m365_copilot_proxy.config import get_settings

#: The key our provider takes in pi's config.
PROVIDER = "m365"

#: Microsoft publishes no context window; these are the estimates the README ships,
#: kept here so the generated file and the example cannot drift apart.
CONTEXT_WINDOW = 128_000
MAX_TOKENS = 16_384

#: Zeroed on purpose: the subscription already paid for these turns.
COST = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}

#: Not decoration — each one describes something this proxy really does. `developer`
#: is accepted but never required, reasoning is chosen by model rather than by an
#: effort parameter, and our SSE chunks carry no usage payload.
COMPAT = {
    "supportsDeveloperRole": False,
    "supportsReasoningEffort": False,
    "supportsUsageInStreaming": False,
}

#: Ids whose picker name is not their slug. Microsoft calls these Automatic, Quick
#: answer and Think deeper; deriving "Magic" from the tone would be a worse label.
DISPLAY_OVERRIDES = {
    "magic": "Auto",
    "chat": "Quick",
    "reasoning": "Think Deeper",
    protocol.DEFAULT_MODEL: "Copilot",
    protocol.IMAGE_MODEL: "Image",
}


class NothingCaptured(RuntimeError):
    """No profile, so there are no models to write."""


def display_name(model_id: str) -> str:
    """A label for pi's `/model` picker, derived from the id.

    `claude-sonnet` -> `M365 Claude Sonnet`, `gpt-5.6-chat` -> `M365 GPT 5.6 Chat`,
    `claude-sonnet-work` -> `M365 Claude Sonnet (Work IQ)`, `agent:sales` ->
    `M365 Agent: sales`.
    """
    if protocol.is_agent_id(model_id):
        return f"M365 Agent: {protocol.agent_slug(model_id)}"

    base = model_id
    suffix = ""
    if base.endswith(protocol.WORK_SUFFIX):
        base, suffix = base[: -len(protocol.WORK_SUFFIX)], " (Work IQ)"

    pretty = DISPLAY_OVERRIDES.get(base)
    if pretty is None:
        words = [w.upper() if w.lower() == "gpt" else w.capitalize() for w in base.split("-")]
        pretty = " ".join(words)
    return f"M365 {pretty}{suffix}"


def model_ids() -> list[str]:
    """Every id worth offering, from the capture alone.

    Not `protocol.available_models()`: that includes the built-in aliases, which
    describe a different tenant at a different time and would fill the picker with
    models yours may not have. A tone the capture saw is a tone that exists.
    """
    profile = tenant_profile.load()
    ids: list[str] = []
    for base in profile.tones:
        ids += [base, f"{base}{protocol.WORK_SUFFIX}"]
    # An agent has no Work IQ toggle and no model picker, so it is offered once.
    ids += [f"{protocol.AGENT_ID_PREFIX}{slug}" for slug in profile.agents]
    return ids


def model_entry(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": display_name(model_id),
        "contextWindow": CONTEXT_WINDOW,
        "maxTokens": MAX_TOKENS,
        "cost": dict(COST),
    }


def base_url() -> str:
    settings = get_settings()
    return f"http://{settings.host}:{settings.port}/v1"


def provider() -> dict[str, Any]:
    """The `providers.m365` block for this tenant."""
    ids = model_ids()
    if not ids:
        raise NothingCaptured(
            "No models captured yet. Run `m365-copilot-proxy capture`, pick each model "
            "in the picker and send it a short message."
        )
    return {
        "baseUrl": base_url(),
        "api": "openai-completions",
        # pi hides models with no auth configured, and the proxy ignores the value:
        # it authenticates to Microsoft with your cached token.
        "apiKey": "unused",
        "compat": dict(COMPAT),
        "models": [model_entry(model_id) for model_id in ids],
    }


def merge_into(existing: Any, block: dict[str, Any]) -> dict[str, Any]:
    """Put our block into a config that may already describe other providers.

    Only `providers.m365` is ours to replace. Anything else in that file was put there
    by hand, and a config generator that eats it would not be worth running twice.
    """
    merged = dict(existing) if isinstance(existing, dict) else {}
    providers = merged.get("providers")
    providers = dict(providers) if isinstance(providers, dict) else {}
    providers[PROVIDER] = block
    merged["providers"] = providers
    return merged
