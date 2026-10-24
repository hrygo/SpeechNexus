"""Every exposed profile entry has shared source/Python/JS acceptance evidence."""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_profile

from tests.support.contracts import document, retrieve

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
    reference = config.get("schema_refs", {}).get(name, f"#/components/schemas/{name}")
    target, _, fragment = reference.partition("#")
    source = document(path.parent / target if target else path)
    source.pop("oneOf", None)
    source["$ref"] = "#" + fragment
    original = Draft202012Validator(
        source, registry=cast(Any, Registry)(retrieve=retrieve), format_checker=FormatChecker()
    )
    value = copy.deepcopy(example["value"])
    before = copy.deepcopy(value)
    assert original.is_valid(value) is example["valid"]
    parser = importlib.import_module(f"speechnexus_contracts.{profile}").parse
    if example["valid"]:
        assert parser(name, value) is value
    else:
        with pytest.raises(ContractValidationError, match=f"^Invalid {name}$"):
            parser(name, value)
    assert value == before


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
