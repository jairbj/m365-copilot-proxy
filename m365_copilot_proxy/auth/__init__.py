"""Authentication against Entra ID for the Substrate (Sydney) audience."""

from m365_copilot_proxy.auth.constants import (
    AUTHORITY,
    CHAT_SCOPES,
    CLIENT_ID,
    IMAGE_SCOPES,
    REDIRECT_URI,
)
from m365_copilot_proxy.auth.tokens import (
    NeedsLoginError,
    TlsTrustError,
    TokenClaims,
    account_summary,
    decode_jwt,
    get_chat_token,
    get_image_token,
    redact,
)

__all__ = [
    "AUTHORITY",
    "CHAT_SCOPES",
    "CLIENT_ID",
    "IMAGE_SCOPES",
    "REDIRECT_URI",
    "NeedsLoginError",
    "TlsTrustError",
    "TokenClaims",
    "account_summary",
    "decode_jwt",
    "get_chat_token",
    "get_image_token",
    "redact",
]
