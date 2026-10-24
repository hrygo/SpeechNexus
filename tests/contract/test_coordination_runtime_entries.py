"""All frozen coordination shapes are consumable; no execution/qualification claims."""

import copy
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads((ROOT / "tests/fixtures/coordination/transaction-examples.json").read_text())


def test_every_source_port_and_result_is_a_packaged_runtime_entrypoint() -> None:
    source = json.loads(
        (ROOT / "contracts/internal/coordination-transactions.schema.json").read_text()
    )
    operations = {
        branch["properties"]["operation"]["const"]
        for branch in source["oneOf"]
        if "properties" in branch
    }
    bundle = json.loads(
        files("speechnexus_contracts").joinpath("coordination.schema.json").read_text()
    )
    assert len(operations) == 18
    assert set(bundle["x-nexus-entrypoints"]) == operations | {"TransactionResult"}


@pytest.mark.parametrize(
    "command", EXAMPLES["valid_commands"], ids=lambda value: value["operation"]
)
def test_runtime_accepts_the_unmodified_shared_request(command: dict[str, Any]) -> None:
    value = copy.deepcopy(command["request"])
    before = copy.deepcopy(value)
    assert validate_coordination(command["operation"], value) is value
    assert value == before


@pytest.mark.parametrize("example", EXAMPLES["invalid_commands"])
def test_runtime_rejects_the_same_shared_negative_request(example: dict[str, Any]) -> None:
    value = copy.deepcopy(example["command"]["request"])
    before = copy.deepcopy(value)
    with pytest.raises(ContractValidationError):
        validate_coordination(example["command"]["operation"], value)
    assert value == before


@pytest.mark.parametrize("result", EXAMPLES["valid_results"])
def test_runtime_accepts_all_shared_typed_result_snapshots(result: dict[str, Any]) -> None:
    value = copy.deepcopy(result)
    assert validate_coordination("TransactionResult", value) is value
    assert value == result
