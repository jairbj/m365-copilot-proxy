"""Token claims and log redaction."""

from __future__ import annotations

import pytest

from m365_copilot_proxy.auth.tokens import decode_jwt, redact
from tests.conftest import make_token


def test_claims_give_the_chathub_path():
    claims = decode_jwt(make_token(oid="abc", tid="xyz"))
    assert claims.chathub_path == "abc@xyz"
    assert claims.audience == "https://substrate.office.com/sydney"
    assert claims.seconds_remaining > 0


def test_a_non_jwt_is_rejected():
    with pytest.raises(ValueError):
        decode_jwt("not-a-jwt")


def test_redaction_strips_the_token_from_a_ws_url():
    url = (
        "wss://substrate.office.com/m365Copilot/Chathub/a@b"
        "?ConversationId=1&access_token=eyJhbGciOi.SECRET&variants=x"
    )
    redacted = redact(url)
    assert "SECRET" not in redacted
    assert "access_token=REDACTED" in redacted
    # Everything else must survive, or the log line stops being useful.
    assert "ConversationId=1" in redacted
    assert "variants=x" in redacted
