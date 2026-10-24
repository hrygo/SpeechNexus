"""The native recognition policy consumes existing language/prompt/temperature domains."""

from pathlib import Path

import pytest

from speechnexus_gateway.domain.asr import AsrPolicy
from tests.contract.test_p0_public_contracts import _load_json, _validator


@pytest.mark.parametrize("language", ["auto", "en", "zh", "zh-Hans"])
@pytest.mark.parametrize("temperature", [0, 1])
@pytest.mark.parametrize("prompt", ["Synthetic.", "x" * 10000])
def test_native_policy_values_match_existing_canonical_parameter_domains(
    language: str, temperature: float, prompt: str
) -> None:
    policy = AsrPolicy("config-1", language=language, prompt=prompt, temperature=temperature)
    document = _load_json(Path("contracts/internal/execution-command.schema.json"))
    for name in ["language", "prompt", "temperature"]:
        _validator(
            {
                "$defs": document["$defs"],
                "$ref": f"#/$defs/AsrEffectiveParameters/properties/{name}",
            }
        ).validate(getattr(policy, name))
