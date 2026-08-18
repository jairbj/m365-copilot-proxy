"""The shipped client configs must stay in step with the models we serve.

A config that names an id the proxy does not know fails quietly: the id falls back
to the default tone and the user gets a different model than the one they picked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from m365_copilot_proxy.bizchat import protocol

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def opencode_model_ids() -> list[str]:
    config = json.loads((EXAMPLES / "opencode.json").read_text())
    return list(config["provider"]["m365"]["models"])


def pi_model_ids() -> list[str]:
    config = json.loads((EXAMPLES / "pi-models.json").read_text())
    return [model["id"] for model in config["providers"]["m365"]["models"]]


@pytest.mark.parametrize("ids", [opencode_model_ids(), pi_model_ids()])
def test_every_advertised_id_resolves_to_a_real_model(ids):
    unknown = [i for i in ids if i not in protocol.available_models()]
    assert not unknown, f"ids the proxy does not serve: {unknown}"


def test_the_two_configs_offer_the_same_models():
    # They document the same proxy; a divergence is an oversight, not a choice.
    assert sorted(opencode_model_ids()) == sorted(pi_model_ids())


def test_every_model_is_offered_in_both_surfaces():
    """The configs are meant to be copied as-is, so the Work IQ variant of each
    model has to be in the picker already — nobody should have to hand-write an id
    to reach their work content."""
    for ids in (opencode_model_ids(), pi_model_ids()):
        base = [i for i in ids if not i.endswith(protocol.WORK_SUFFIX)]
        work = {i for i in ids if i.endswith(protocol.WORK_SUFFIX)}
        missing = [i for i in base if f"{i}{protocol.WORK_SUFFIX}" not in work]
        assert not missing, f"models with no Work IQ variant: {missing}"


def test_the_work_variants_resolve_to_their_base_model():
    # A suffix that did not parse would quietly serve the default tone.
    for ids in (opencode_model_ids(), pi_model_ids()):
        for model_id in (i for i in ids if i.endswith(protocol.WORK_SUFFIX)):
            base, work_iq = protocol.parse_model(model_id)
            assert work_iq is True
            assert base in protocol.model_tones()


def test_pi_declares_the_compat_flags_that_match_this_proxy():
    provider = json.loads((EXAMPLES / "pi-models.json").read_text())["providers"]["m365"]
    compat = provider["compat"]
    # We put the system prompt in a `system` message, never `developer`.
    assert compat["supportsDeveloperRole"] is False
    # Our SSE chunks carry no `usage` payload.
    assert compat["supportsUsageInStreaming"] is False
    assert provider["api"] == "openai-completions"


def test_both_configs_point_at_the_documented_default_port():
    opencode = json.loads((EXAMPLES / "opencode.json").read_text())
    pi = json.loads((EXAMPLES / "pi-models.json").read_text())
    expected = "http://127.0.0.1:8765/v1"
    assert opencode["provider"]["m365"]["options"]["baseURL"] == expected
    assert pi["providers"]["m365"]["baseUrl"] == expected
