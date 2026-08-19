"""The shipped pi config must stay in step with the models we serve.

A config that names an id the proxy does not know fails quietly: the id falls back
to the default tone and the user gets a different model than the one they picked.

This file is the hand-written fallback for anyone who has not run `capture` yet.
The generated one — `m365-copilot-proxy pi-config` — is covered in test_pi_config.py,
and both are held to the same shape.
"""

from __future__ import annotations

import json
from pathlib import Path

from m365_copilot_proxy import pi_config
from m365_copilot_proxy.bizchat import protocol

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def pi_provider() -> dict:
    return json.loads((EXAMPLES / "pi-models.json").read_text())["providers"]["m365"]


def pi_model_ids() -> list[str]:
    return [model["id"] for model in pi_provider()["models"]]


def test_every_advertised_id_resolves_to_a_real_model():
    unknown = [i for i in pi_model_ids() if i not in protocol.available_models()]
    assert not unknown, f"ids the proxy does not serve: {unknown}"


def test_every_model_is_offered_in_both_surfaces():
    """The file is meant to be copied as-is, so the Work IQ variant of each model has
    to be in the picker already — nobody should have to hand-write an id to reach
    their work content."""
    ids = pi_model_ids()
    base = [i for i in ids if not i.endswith(protocol.WORK_SUFFIX)]
    work = {i for i in ids if i.endswith(protocol.WORK_SUFFIX)}
    missing = [i for i in base if f"{i}{protocol.WORK_SUFFIX}" not in work]
    assert not missing, f"models with no Work IQ variant: {missing}"


def test_the_work_variants_resolve_to_their_base_model():
    # A suffix that did not parse would quietly serve the default tone.
    for model_id in (i for i in pi_model_ids() if i.endswith(protocol.WORK_SUFFIX)):
        base, work_iq = protocol.parse_model(model_id)
        assert work_iq is True
        assert base in protocol.model_tones()


def test_it_declares_the_compat_flags_that_match_this_proxy():
    provider = pi_provider()
    # We put the system prompt in a `system` message, never `developer`.
    assert provider["compat"]["supportsDeveloperRole"] is False
    # Our SSE chunks carry no `usage` payload.
    assert provider["compat"]["supportsUsageInStreaming"] is False
    assert provider["api"] == "openai-completions"


def test_it_points_at_the_documented_default_port():
    assert pi_provider()["baseUrl"] == "http://127.0.0.1:8765/v1"


def test_the_example_and_the_generator_agree_on_the_shape():
    # Two ways of producing the same file must not drift: a name written by hand and
    # one derived from the id have to match, or `pi-config` would silently rename
    # every model of anyone who had been using the example.
    example = pi_provider()
    assert set(example) == {"baseUrl", "api", "apiKey", "compat", "models"}
    assert example["compat"] == pi_config.COMPAT
    for model in example["models"]:
        assert set(model) == {"id", "name", "contextWindow", "maxTokens", "cost"}
        assert model["name"] == pi_config.display_name(model["id"])
        assert model["contextWindow"] == pi_config.CONTEXT_WINDOW
        assert model["maxTokens"] == pi_config.MAX_TOKENS
        assert model["cost"] == pi_config.COST
