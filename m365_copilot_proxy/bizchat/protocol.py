"""Wire constants for the Microsoft 365 Copilot BizChat surface.

Every list here mirrors what the real `m365.cloud.microsoft/chat` web client sends.
They are not decorative: the server gates features on them. `variants` and
`optionsSets` unlock server-side capabilities, and `allowedMessageTypes` is a
declare-to-receive contract — the server only emits a frame kind the client said it
can handle, so omitting an entry silently removes the feature rather than erroring.
"""

from __future__ import annotations

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
    DEFAULT_MODEL: "magic",
    "auto": "magic",
    IMAGE_MODEL: "magic",
    # Generic modes
    "quick": "Gpt_Quick",
    "think-deeper": "Gpt_Reasoning",
    # Claude (genuine Anthropic models on the Copilot subscription)
    "claude": "Claude_Sonnet",
    "claude-sonnet": "Claude_Sonnet",
    "claude-sonnet-4.5": "Claude_Sonnet",
    "claude-sonnet-think-deeper": "Claude_Sonnet_Reasoning",
    "claude-opus": "Claude_Opus",
    # GPT-5.5 / 5.6
    "gpt-5.5": "Gpt_5_5_Chat",
    "gpt-5.5-quick": "Gpt_5_5_Chat",
    "gpt-5.5-think-deeper": "Gpt_5_5_Reasoning",
    "gpt-5.6-think-deeper": "Gpt_5_6_Reasoning",
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


def tone_for_model(model: str | None) -> str:
    """Resolve a requested model id to a `tone` the server accepts.

    An unmapped `claude-*` id (clients send things like `claude-opus-4-8`) must not
    fall back to the GPT tone — it would serve GPT under a Claude name. Route it to
    the Claude tone instead; everything else gets the default.
    """
    if not model:
        return MODEL_TONES[DEFAULT_MODEL]
    exact = MODEL_TONES.get(model)
    if exact:
        return exact
    if model.lower().startswith("claude"):
        return "Claude_Sonnet"
    return MODEL_TONES[DEFAULT_MODEL]


def available_models() -> list[str]:
    return list(MODEL_TONES)
