"""Every exposed profile entry has shared source/Python/JS acceptance evidence."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_profile

from tests.support.contract_examples import example_value
from tests.support.contracts import retrieve

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "contracts/bindings.json").read_text())
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/consumer-profiles.json").read_text())[
    "examples"
]


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example["id"])
def test_consumer_profile_agrees_with_original(example: dict[str, Any]) -> None:
    profile, name = example["profile"], example["schema"]
    config = MANIFEST["profiles"][profile]
    path = ROOT / "contracts" / config["source"]

    def reference_schema(reference: str) -> dict[str, str]:
        target, _, fragment = reference.partition("#")
        source_path = (path.parent / target if target else path).resolve()
        return {"$ref": source_path.as_uri() + "#" + fragment}

    source: dict[str, Any]
    if name in config.get("schema_unions", {}):
        group = config["schema_unions"][name]
        source = {
            "allOf": [
                reference_schema(group["base"]),
                {"oneOf": [reference_schema(ref) for ref in group["members"]]},
            ]
        }
    else:
        reference = config.get("schema_refs", {}).get(
            name, "#" + config.get("definitions_prefix", "/components/schemas/") + name
        )
        source = reference_schema(reference)
    original = Draft202012Validator(
        source, registry=cast(Any, Registry)(retrieve=retrieve), format_checker=FormatChecker()
    )
    value = example_value(example)
    before = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).digest()
    accepted = original.is_valid(value)
    assert accepted is example["valid"]
    parser = importlib.import_module(f"speechnexus_contracts.{profile}").parse
    if example["valid"]:
        same_identity = parser(name, value) is value
        assert same_identity
    else:
        with pytest.raises(ContractValidationError, match=f"^Invalid {name}$"):
            parser(name, value)
    assert hashlib.sha256(json.dumps(value, sort_keys=True).encode()).digest() == before


def test_profile_coverage_and_namespace_boundaries() -> None:
    for profile, config in MANIFEST["profiles"].items():
        for name in config["schemas"]:
            examples = [e for e in EXAMPLES if e["profile"] == profile and e["schema"] == name]
            assert any(e["valid"] for e in examples), (profile, name)
            assert any(not e["valid"] for e in examples), (profile, name)
        with pytest.raises(ValueError, match="Unknown profile contract"):
            validate_profile(profile, "NotAContract", {})
    with pytest.raises(ValueError, match="Unknown contract profile"):
        validate_profile("../../private", "anything", {})
    with pytest.raises(ValueError, match="Unknown profile contract"):
        validate_profile("workbench", "SubmitExecutionRequest", {})


def test_python_nested_refinements_preserve_required_base_resource_shape() -> None:
    from typing import get_args

    from speechnexus_contracts import execution_models as models

    for alias in (
        models.ExternalInternalExecutionCommandSchemaJsonTtsCommand,
        models.ExternalInternalExecutionCommandSchemaJsonVoiceValidationCommand,
    ):
        shape = getattr(alias, "__value__", alias)
        for command in get_args(shape) or (shape,):
            assert command.__required_keys__ >= {"schema_version", "kind", "resources"}
    assert models.ExternalInternalExecutionCommandSchemaJsonResolvedResources.__required_keys__ >= {
        "voice",
        "voice_material",
        "vocabulary",
        "pronunciation",
        "voice_kind",
    }
