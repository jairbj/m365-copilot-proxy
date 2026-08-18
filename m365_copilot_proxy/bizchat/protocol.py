"""Wire constants for the Microsoft 365 Copilot BizChat surface.

Every list here mirrors what the real `m365.cloud.microsoft/chat` web client sends.
They are not decorative: the server gates features on them. `variants` and
`optionsSets` unlock server-side capabilities, and `allowedMessageTypes` is a
declare-to-receive contract — the server only emits a frame kind the client said it
can handle, so omitting an entry silently removes the feature rather than erroring.

These are defaults, not truths: they describe one tenant at one moment. A captured
tenant profile (`m365-copilot-proxy capture`) overrides them — see `profile.py` and
the accessor functions at the bottom of this module, which are what callers should
use rather than reading the constants directly.
"""

from __future__ import annotations

import logging

from m365_copilot_proxy.bizchat import profile as tenant_profile

log = logging.getLogger(__name__)

WS_HOST = "substrate.office.com"
WS_PATH = "/m365Copilot/Chathub"

#: The server refuses the upgrade without a real browser Origin and User-Agent.
ORIGIN = "https://m365.cloud.microsoft"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

#: Feature flags passed in the WebSocket query string.
VARIANTS = [
    "EnableMcpServerWidgets",
    "feature.EnableMcpServerWidgets",
    "feature.EnableLuForChatCIQ",
    "feature.enableChatCIQPlugin",
    "EnableRequestPlugins",
    "feature.EnableSensitivityLabels",
    "EnableUnsupportedUrlDetector",
    "feature.IsCustomEngineCopilotEnabled",
    "feature.bizchatfluxv3",
    "feature.enablechatpages",
    "feature.enableCodeCanvas",
    "feature.turnOnWorkTabRecommendation",
    "turnOffWorkTabUpsellFromClient",
    "feature.turnOnDARecommendation",
    "feature.IsStreamingModeInChatRequestEnabled",
    "IncludeSourceAttributionsConcise",
    "SkipPublishEmptyMessage",
    "feature.EnableDeduplicatingSourceAttributions",
    "Enable3PActionProgressMessages",
    "feature.enableClientWebRtc",
    "feature.EnableMeetingRecapOfSeriesMeetingWithCiq",
    "feature.EnableReferencesListCompleteSignal",
    "feature.StorageMessageSplitDisabled",
    "feature.EnableCuaTakeControlApi",
    "feature.cwcallowedos",
    "feature.disabledisallowedmsgs",
    "feature.enableCitationsForSynthesisData",
    "feature.enableGenerateGraphicArtOptionsSet",
    "cdximagen",
    "feature.EnableUpdatedUXForConfirmationDialog",
    "feature.EnableClientFileURLSupportForOfficeWebPaidCopilot",
    "feature.EnableDesignEditorImageGrounding",
    "feature.EnableDesignerEditor",
    "feature.OfficeWebToHelix",
    "feature.OfficeDesktopToHelix",
    "feature.M365TeamsHubToHelix",
    "feature.OwaHubToHelix",
    "feature.MonarchHubToHelix",
    "feature.Win32OutlookHubToHelix",
    "feature.MacOutlookHubToHelix",
    "Agt_bizchat_enableGpt5ForHelix",
]

#: The `agent` field names the surface. It is the visible half of the "Work IQ"
#: toggle in the web client: with work grounding on the client sends `work` (plus a
#: matching `scenario`, `variants` and `enterprise_*` optionsSets), and with it off
#: it sends `web` (with the consumer `cwc_*` family instead). The rest of the
#: surface is learned by `capture`, not hard-coded.
AGENT_WORK = "work"
AGENT_WEB = "web"

#: Model id suffixes selecting the surface per request.
WORK_SUFFIX = "-work"
WEB_SUFFIX = "-web"

#: Model id prefix selecting a captured declarative agent — a custom agent built in
#: the Copilot UI, entered through `threadLevelGptId`. Not to be confused with the
#: `agent` QUERY field above, which names the Work IQ surface. An agent id takes no
#: Work IQ suffix and no tone: the agent UI offers neither.
AGENT_ID_PREFIX = "agent:"

#: Static query-string fields identifying the client surface.
QUERY_DEFAULTS = {
    "source": '"officeweb"',
    "product": "Office",
    "agentHost": "Bizchat.FullScreen",
    "licenseType": "Starter",
    "agent": "web",
    "scenario": "OfficeWebIncludedCopilot",
}

#: Unlocks M365's server-side Python sandbox: the model writes and actually
#: EXECUTES code, returning real results rather than predicted ones.
CODE_INTERPRETER_OPTIONS_SETS = [
    "cwc_code_interpreter",
    "cwc_code_interpreter_amsfix",
    "cwc_code_interpreter_citation_fix",
    "code_interpreter_interactive_charts",
    "code_interpreter_matplotlib_patching",
]

#: The image-generation set the official web client sends wholesale, letting the
#: model pick the artifact type from the prompt. `flux_v3` is BizChat's
#: orchestration codename, not the model. `...non_watermarked_storage` keeps the
#: artifact unwatermarked. Excludes the GPT-V/upload family, which is image *input*.
IMAGE_GEN_OPTIONS_SETS = [
    "cwc_flux_image",
    "cwc_flux_v3",
    "enable_gg_gpt",
    "flux_v3_progress_messages",
    "flux_v3_image_gen_enable_dimensions",
    "flux_v3_image_gen_enable_icon_dimensions",
    "flux_v3_image_gen_enable_story",
    "flux_v3_image_gen_enable_designer_dimensions_meta_prompting_in_system_prompts",
    "flux_v3_image_gen_enable_system_text_with_params",
    "flux_v3_image_gen_enable_non_watermarked_storage",
]

#: Frame kinds we tell the server we can handle. Declare-to-receive.
ALLOWED_MESSAGE_TYPES = [
    "Chat",
    "Suggestion",
    "InternalSearchQuery",
    "Disengaged",
    "InternalLoaderMessage",
    "Progress",
    "RenderCardRequest",
    "SemanticSerp",
    "GenerateContentQuery",
    "SearchQuery",
    "ConfirmationCard",
    "DeveloperLogs",
    "EndOfRequest",
    "ReferencesListComplete",
    "GeneratedCode",
]

IMAGE_MESSAGE_TYPE = "GenerateGraphicArt"

CLIENT_INFO = {
    "clientPlatform": "mcmcopilot-web",
    "clientAppName": "Office",
    "clientEntrypoint": "mcmcopilot-officeweb",
    "clientAppType": "Web",
    "deviceOS": "Linux",
    "deviceType": "Desktop",
}

ENTITY_ANNOTATION_TYPES = ["People", "File", "Event", "Email", "TeamsMessage"]

#: Built-in plugin the plain-chat path enables, matching the web client.
BING_PLUGIN = {"Id": "BingWebSearch", "Source": "BuiltIn"}

DEFAULT_MODEL = "m365-copilot"
IMAGE_MODEL = "m365-copilot-image"

#: Model id -> `tone`, the field that actually selects the backing model.
#:
#: The server VALIDATES tones: an unknown value fails the turn with
#: "Failed to invoke 'Chat'", so only confirmed-accepted values belong here.
MODEL_TONES: dict[str, str] = {
    # --- Observed on a real work tenant (August 2026), by `capture`. These ids are
    # the ones the examples use, and they are the tone slugs the client itself
    # sends, so they are the closest thing to ground truth this map has.
    "magic": "Magic",
    "chat": "Chat",
    "reasoning": "Reasoning",
    "claude-sonnet": "Claude_Sonnet",
    "claude-opus": "Claude_Opus",
    "gpt-5.6-reasoning": "Gpt_5_6_Reasoning",
    "gpt-5.6-chat": "Gpt_5_6_Chat",
    "gpt-5.5-chat": "Gpt_5_5_Chat",
    # --- Friendly aliases and older entries inherited from the reference
    # implementation. They describe a different tenant at a different time, so a
    # capture overrides them; keep them as the pre-capture fallback.
    DEFAULT_MODEL: "magic",
    "auto": "magic",
    IMAGE_MODEL: "magic",
    # Generic modes
    "quick": "Gpt_Quick",
    "think-deeper": "Gpt_Reasoning",
    # Claude (genuine Anthropic models on the Copilot subscription)
    "claude": "Claude_Sonnet",
    "claude-sonnet-4.5": "Claude_Sonnet",
    "claude-sonnet-think-deeper": "Claude_Sonnet_Reasoning",
    # GPT-5.5 / 5.6
    "gpt-5.5": "Gpt_5_5_Chat",
    "gpt-5.5-quick": "Gpt_5_5_Chat",
    "gpt-5.5-think-deeper": "Gpt_5_5_Reasoning",
    "gpt-5.6-think-deeper": "Gpt_5_6_Reasoning",
    # `gpt-5.6-quick` was inferred from the naming pattern and the capture DISPROVED
    # it: the real quick variant is `gpt-5.6-chat` -> `Gpt_5_6_Chat`, above. Kept as
    # an alias so anyone who copied the old id still lands somewhere real.
    "gpt-5.6-quick": "Gpt_5_6_Chat",
    # Older generations, still accepted
    "gpt-5.4": "Gpt_5_4_Reasoning",
    "gpt-5.4-quick": "Gpt_5_4_Quick",
    "gpt-5.4-think-deeper": "Gpt_5_4_Reasoning",
    "gpt-5.3": "Gpt_5_3_Quick",
    "gpt-5.3-quick": "Gpt_5_3_Quick",
    "gpt-5.3-think-deeper": "Gpt_5_3_Reasoning",
    "gpt-5.2": "Gpt_5_2_Quick",
    "gpt-5.2-quick": "Gpt_5_2_Quick",
    "gpt-5.2-think-deeper": "Gpt_5_2_Reasoning",
}

#: Server-side cap on user messages in one ConversationId.
MAX_MESSAGES_PER_CONVERSATION = 600


# --- Effective values: built-in defaults, overridden by a captured profile ---


def model_tones() -> dict[str, str]:
    """The built-in tone map, with anything the capture learned layered on top."""
    return {**MODEL_TONES, **tenant_profile.load().tones}


def declarative_agents() -> dict[str, tenant_profile.DeclarativeAgent]:
    """The declarative agents `capture` recorded, by slug."""
    return tenant_profile.load().agents


def is_agent_id(model: str | None) -> bool:
    return bool(model) and model.startswith(AGENT_ID_PREFIX)  # type: ignore[union-attr]


def agent_slug(model: str | None) -> str:
    """The slug inside an `agent:<slug>` id, or "" for anything else."""
    return model[len(AGENT_ID_PREFIX) :] if is_agent_id(model) else ""


def agent_for_model(model: str | None) -> tenant_profile.DeclarativeAgent | None:
    """The captured agent an id names, or None — including for a plain model id.

    None for an `agent:` id that was never captured is deliberate: the caller turns
    that into an error rather than quietly serving plain Copilot under the agent's
    name.
    """
    agent = declarative_agents().get(agent_slug(model)) if is_agent_id(model) else None
    return agent if agent is not None and not agent.is_empty else None


def _agent_surface(
    agent: tenant_profile.DeclarativeAgent | None,
) -> tenant_profile.Surface | None:
    return agent.surface if agent is not None and not agent.surface.is_empty else None


def surface_name(work_iq: bool | None) -> str:
    """Which captured surface a Work IQ choice corresponds to.

    `None` means "whatever the configuration says", which is off unless the user
    turned it on. Resolving it here rather than at each call site is what keeps a
    caller from silently grounding a turn in work content by leaving the argument
    out.
    """
    from m365_copilot_proxy.config import get_settings

    wants_work = get_settings().work_iq if work_iq is None else work_iq
    return tenant_profile.WORK if wants_work else tenant_profile.WEB


def _surface(work_iq: bool | None) -> tenant_profile.Surface | None:
    """The captured surface to serve, warning when we have to substitute.

    Work IQ swaps the whole client shape, not one field, so mixing the `agent` of
    one surface with the `optionsSets` of the other would send a combination no real
    client sends. Better to serve the surface we have and say so.
    """
    wanted = surface_name(work_iq)
    profile = tenant_profile.load()
    if profile.has_surface(wanted):
        return profile.surfaces[wanted]

    substitute = profile.surface_for(wanted)
    if substitute is not None:
        log.warning(
            "No captured '%s' surface — using the '%s' one instead. Run `capture` "
            "with Work IQ %s in the chat window to record it.",
            wanted,
            substitute.query.get("agent", "?"),
            "on" if wanted == tenant_profile.WORK else "off",
        )
    return substitute


def query_defaults(
    work_iq: bool | None = None,
    agent: tenant_profile.DeclarativeAgent | None = None,
) -> dict[str, str]:
    """Static WebSocket query fields for the requested surface.

    This is where a work tenant diverges from an individual one, and where Work IQ
    on diverges from off (`agent`, `scenario`, and the whole `variants` string) —
    exactly the kind of thing that is not worth guessing.

    A declarative agent replaces the lot: it was recorded as one whole connection,
    so it is replayed as one rather than merged with a Work IQ choice its UI does
    not offer.
    """
    agent_surface = _agent_surface(agent)
    if agent_surface is not None:
        return dict(agent_surface.query)

    captured = (_surface(work_iq) or tenant_profile.Surface()).query
    merged = dict(QUERY_DEFAULTS)
    for key in (*QUERY_DEFAULTS, "isEdu"):
        if key in captured:
            merged[key] = captured[key]
    if not captured:
        # Nothing captured yet: the agent field is the one part of the surface we
        # can set from knowledge alone.
        merged["agent"] = (
            AGENT_WORK if surface_name(work_iq) == tenant_profile.WORK else AGENT_WEB
        )
    return merged


def variants(
    work_iq: bool | None = None,
    agent: tenant_profile.DeclarativeAgent | None = None,
) -> str:
    """The comma-separated feature variants for the requested surface."""
    surface = _agent_surface(agent) or _surface(work_iq)
    captured = surface.query.get("variants") if surface else None
    return captured if captured else ",".join(VARIANTS)


def option_sets(
    *,
    work_iq: bool | None = None,
    generate_images: bool = False,
    agent: tenant_profile.DeclarativeAgent | None = None,
) -> list[str]:
    """The optionsSets to send for the requested surface."""
    surface = _agent_surface(agent) or _surface(work_iq)
    captured = surface.option_sets if surface else []
    sets = list(captured) if captured else list(CODE_INTERPRETER_OPTIONS_SETS)
    if generate_images:
        sets += [s for s in IMAGE_GEN_OPTIONS_SETS if s not in sets]
    return sets


def allowed_message_types(
    *,
    work_iq: bool | None = None,
    generate_images: bool = False,
    agent: tenant_profile.DeclarativeAgent | None = None,
) -> list[str]:
    """The message types we declare we can handle for the requested surface."""
    surface = _agent_surface(agent) or _surface(work_iq)
    captured = surface.allowed_message_types if surface else []
    types = list(captured) if captured else list(ALLOWED_MESSAGE_TYPES)
    if generate_images and IMAGE_MESSAGE_TYPE not in types:
        types.append(IMAGE_MESSAGE_TYPE)
    return types


def plugins(
    work_iq: bool | None = None,
    agent: tenant_profile.DeclarativeAgent | None = None,
) -> list[dict[str, str]]:
    """The plugin list for the requested surface, captured if available.

    An empty captured list means "this surface sends no plugins" and is honoured;
    only a surface that never recorded them (`None`) falls back to the built-in.
    """
    surface = _agent_surface(agent) or _surface(work_iq)
    if surface is not None and surface.plugins is not None:
        return [dict(p) for p in surface.plugins]
    return [dict(BING_PLUGIN)]


def parse_model(model: str | None) -> tuple[str, bool | None]:
    """Split a model id into its base and an explicit Work IQ choice.

    `claude-sonnet-work` -> ("claude-sonnet", True)
    `claude-sonnet-web`  -> ("claude-sonnet", False)
    `claude-sonnet`      -> ("claude-sonnet", None), meaning "caller's default"

    A model id that actually exists is never split: if Microsoft ever ships a tone
    whose slug ends in `-work`, the real model wins over the suffix convention.
    """
    if not model:
        return "", None
    if is_agent_id(model):
        # An agent has no Work IQ toggle, so a slug ending in `-work` is a name.
        return model, None
    if model in model_tones():
        return model, None
    for suffix, work_iq in ((WORK_SUFFIX, True), (WEB_SUFFIX, False)):
        if model.endswith(suffix) and len(model) > len(suffix):
            return model[: -len(suffix)], work_iq
    return model, None


def tone_for_model(model: str | None) -> str | None:
    """Resolve a requested model id to a `tone` the server accepts.

    An unmapped `claude-*` id (clients send things like `claude-opus-4-8`) must not
    fall back to the GPT tone — it would serve GPT under a Claude name. Route it to
    the Claude tone instead; everything else gets the default.

    An agent id resolves to whatever tone the real client sent for that agent, which
    is often None — the agent UI has no model picker, and inventing a tone it never
    sends is exactly the guess `capture` exists to avoid.
    """
    if is_agent_id(model):
        agent = agent_for_model(model)
        return agent.tone if agent is not None else None

    tones = model_tones()
    default = tones.get(DEFAULT_MODEL, MODEL_TONES[DEFAULT_MODEL])
    if not model:
        return default
    exact = tones.get(model)
    if exact:
        return exact
    if model.lower().startswith("claude"):
        return "Claude_Sonnet"
    return default


def available_models() -> list[str]:
    """Every model id, each also offered with the Work IQ suffix.

    The `-web` suffix is accepted on input but not advertised — it would be a third
    copy of the list saying what the default already says. Declarative agents are
    listed once each: they have neither a Work IQ toggle nor a model picker.
    """
    base = list(model_tones())
    agents = [f"{AGENT_ID_PREFIX}{slug}" for slug in declarative_agents()]
    return base + [f"{name}{WORK_SUFFIX}" for name in base] + agents
