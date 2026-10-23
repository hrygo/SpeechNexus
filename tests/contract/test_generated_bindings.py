"""Shared wire examples must agree with Python and original source schemas."""

from __future__ import annotations

import copy
import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate

from tests.support.contracts import document, retrieve

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES: list[dict[str, Any]] = json.loads(
    (ROOT / "tests/fixtures/contracts/file-workflow.json").read_text()
)["examples"]


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example["id"])
def test_generated_python_preserves_source_validation(example: dict[str, Any]) -> None:
    from referencing import Registry

    source = document(ROOT / "contracts/speech-openapi.yaml")
    source["$ref"] = f"#/components/schemas/{example['schema']}"
    original = Draft202012Validator(
        source, registry=cast(Any, Registry)(retrieve=retrieve), format_checker=FormatChecker()
    )
    value = copy.deepcopy(example["value"])
    before = copy.deepcopy(value)
    assert original.is_valid(value) is example["valid"], example["id"]
    if example["valid"]:
        assert validate(example["schema"], value) is value
    else:
        with pytest.raises(ContractValidationError, match=f"^Invalid {example['schema']}$"):
            validate(example["schema"], value)
    assert value == before, "Boundary validation must not coerce or mutate input"


def test_generated_entrypoints_have_shared_positive_and_negative_examples() -> None:
    manifest = json.loads((ROOT / "contracts/bindings.json").read_text())
    for name in manifest["schemas"]:
        assert any(e["schema"] == name and e["valid"] for e in EXAMPLES), name
        assert any(e["schema"] == name and not e["valid"] for e in EXAMPLES), name


RECOVERY_EXAMPLES: list[dict[str, Any]] = json.loads(
    (ROOT / "tests/fixtures/contracts/execution-recovery.json").read_text()
)["examples"]


@pytest.mark.parametrize("example", RECOVERY_EXAMPLES, ids=lambda example: example["id"])
def test_recovery_coordination_bundle_matches_the_source(example: dict[str, Any]) -> None:
    from speechnexus_contracts.validation import validate_coordination

    from tests.support.contracts import validator

    value = copy.deepcopy(example["value"])
    before = copy.deepcopy(value)
    original = validator("internal/coordination-transactions.schema.json", example["schema"])
    assert original.is_valid(value) is example["valid"]
    if example["valid"]:
        assert validate_coordination(example["schema"], value) is value
    else:
        with pytest.raises(ContractValidationError, match=f"^Invalid {example['schema']}$"):
            validate_coordination(example["schema"], value)
    assert value == before


@pytest.mark.parametrize(
    "filename",
    ["file.schema.json", "coordination.schema.json"]
    + [
        f"{profile}.schema.json"
        for profile in json.loads((ROOT / "contracts/bindings.json").read_text())["profiles"]
    ],
)
def test_packaged_bundle_is_closed_and_unknown_contracts_fail(filename: str) -> None:
    bundle = json.loads(files("speechnexus_contracts").joinpath(filename).read_text())
    Draft202012Validator.check_schema(bundle)

    def check(node: Any) -> None:
        if isinstance(node, dict):
            assert "$id" not in node
            if "$ref" in node:
                assert node["$ref"].startswith("#/$defs/")
                assert node["$ref"].removeprefix("#/$defs/") in bundle["$defs"]
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(bundle)
    with pytest.raises(ValueError, match="Unknown file contract"):
        validate("NotAContract", {})


def test_generated_python_overloads_and_allof_refinements_are_usable() -> None:
    from speechnexus_contracts import parse
    from speechnexus_contracts.models import JobQueuedEvent, TtsJobCreate

    job = parse("JobCreateRequest", EXAMPLES[1]["value"])
    assert job["kind"] == "tts"
    assert TtsJobCreate.__required_keys__ >= {"kind", "model", "input", "voice"}
    assert JobQueuedEvent.__required_keys__ >= {"id", "type", "data", "job_id", "occurred_at"}


def test_coordination_ownership_covers_ports_tables_and_query_snapshots() -> None:
    matrix = json.loads((ROOT / "contracts/coordination-ownership.json").read_text())
    schema = json.loads((ROOT / "contracts" / matrix["contract"]).read_text())
    ports = {
        branch["properties"]["operation"]["const"]
        for branch in schema["oneOf"]
        if "properties" in branch
    }
    assert ports == set(matrix["ports"])
    ddl = (ROOT / "migrations/0001_cluster_coordination_draft.sql").read_text()
    assert set(re.findall(r"^CREATE TABLE (\w+)", ddl, re.MULTILINE)) == set(matrix["tables"])
    for name, port in matrix["ports"].items():
        assert port["producer"] and port["consumer"]
        assert all(matrix["tables"][table] == port["owner"] for table in port["writes"]), name
    branches = schema["$defs"]["query_outcome"]["properties"]["target"]["oneOf"]
    assert {
        branch["properties"]["kind"]["const"]: branch["x-nexus-result-kind"] for branch in branches
    } == {target: details["snapshot"] for target, details in matrix["query_outcome"].items()}
