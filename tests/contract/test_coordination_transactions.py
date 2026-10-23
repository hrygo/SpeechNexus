"""Frozen port shapes, boundary examples and target/legacy separation."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema.exceptions import ValidationError

from tests.support.contracts import validator
from tests.support.coordination import digest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES: dict[str, Any] = json.loads(
    (ROOT / "tests/fixtures/coordination/transaction-examples.json").read_text()
)


@pytest.mark.parametrize("command", EXAMPLES["valid_commands"], ids=lambda c: c["operation"])
def test_every_transaction_port_has_valid_producer_consumer_example(
    command: dict[str, Any],
) -> None:
    validator("internal/coordination-transactions.schema.json").validate(command)
    validator("internal/coordination-transactions.schema.json", command["operation"]).validate(
        command["request"]
    )
    if command["operation"] == "create_submit":
        wire = command["request"]["execution_request"]
        assert wire["command_digest"] == digest(wire["command"])


@pytest.mark.parametrize("example", EXAMPLES["invalid_commands"])
def test_transaction_port_rejects_unknown_fields_and_invalid_boundaries(
    example: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        validator("internal/coordination-transactions.schema.json").validate(example["command"])


def test_every_port_is_covered_and_reuses_execution_runtime_domains() -> None:
    schema = json.loads(
        (ROOT / "contracts/internal/coordination-transactions.schema.json").read_text()
    )
    operations = {
        branch["properties"]["operation"]["const"]
        for branch in schema["oneOf"]
        if "properties" in branch
    }
    assert operations == {example["operation"] for example in EXAMPLES["valid_commands"]}
    assert len(operations) == 18
    assert (
        set(schema["$defs"]["TransactionResult"]["properties"]["operation"]["enum"]) == operations
    )
    commands = json.loads((ROOT / "contracts/internal/execution-command.schema.json").read_text())
    assert {
        command["request"]["execution_request"]["command"]["kind"]
        for command in EXAMPLES["valid_commands"]
        if command["operation"] == "create_submit"
    } == {example["kind"] for example in commands["examples"]}
    definitions = schema["$defs"]
    assert definitions["create_submit"]["properties"]["execution_request"]["$ref"] == (
        "./execution-control.openapi.yaml#/components/schemas/SubmitExecutionRequest"
    )
    assert definitions["create_runtime_intent"]["properties"]["request"]["$ref"] == (
        "./runtime-control.openapi.yaml#/components/schemas/RuntimeInstanceIntentRequest"
    )
    assert definitions["LeaseSnapshot"]["properties"]["cleanup_state"]["$ref"] == (
        "../shared/operation-states.schema.json#/$defs/CleanupState"
    )


def test_transaction_result_unknown_is_not_a_safe_retry() -> None:
    contract = validator("internal/coordination-transactions.schema.json", "TransactionResult")
    for result in EXAMPLES["valid_results"]:
        contract.validate(result)
        validator("internal/coordination-transactions.schema.json").validate(result)
    unknown = copy.deepcopy(EXAMPLES["valid_results"][1])
    unknown["recovery"] = "retry_transaction"
    with pytest.raises(ValidationError):
        contract.validate(unknown)
    committed = copy.deepcopy(EXAMPLES["valid_results"][0])
    del committed["value"]
    with pytest.raises(ValidationError):
        contract.validate(committed)
    mismatched = copy.deepcopy(EXAMPLES["valid_results"][0])
    mismatched["value"] = {
        "kind": "fact",
        "fact_id": "fact-test",
        "request_digest": "sha256:" + "a" * 64,
    }
    with pytest.raises(ValidationError):
        contract.validate(mismatched)


@pytest.mark.parametrize("field", ["execution_stopped", "resources_released"])
def test_verified_stop_never_accepts_unconfirmed_facts(field: str) -> None:
    command = copy.deepcopy(
        next(c for c in EXAMPLES["valid_commands"] if c["operation"] == "record_stop")
    )
    command["request"]["evidence"][field] = False
    with pytest.raises(ValidationError):
        validator("internal/coordination-transactions.schema.json").validate(command)


def test_active_ddl_has_no_replaced_pg_business_mechanisms() -> None:
    active = (ROOT / "migrations/0001_cluster_coordination_draft.sql").read_text()
    legacy = (ROOT / "tests/fixtures/coordination/legacy_postgresql.sql").read_text()
    assert len(re.findall(r"^CREATE TABLE ", active, re.MULTILINE)) == 15
    for mechanism in (
        r"CREATE (?:CONSTRAINT )?TRIGGER",
        r"CREATE FUNCTION",
        r"CREATE EXTENSION",
        r"EXCLUDE USING",
        r"CREATE (?:UNIQUE )?INDEX[^\n]*\n?[^\n]*WHERE",
        r"SECURITY DEFINER",
        r"capacity_verification_queue",
    ):
        assert not re.search(mechanism, active)
    assert "CREATE FUNCTION enqueue_capacity_verification()" in legacy
    assert "REVOKE ALL ON FUNCTION enqueue_capacity_verification() FROM PUBLIC" in legacy
    assert "TG_RELID NOT IN" in legacy
    handoff = (ROOT / "docs/architecture/coordination-transactions.md").read_text()
    functions = re.findall(r"^CREATE FUNCTION ([a-z_]+)\(", legacy, re.MULTILINE)
    assert len(functions) == 24
    assert all(f"`{name}`" in handoff for name in functions)
