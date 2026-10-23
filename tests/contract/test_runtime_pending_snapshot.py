"""A durable intent must not invent a controller observation or release evidence."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from tests.support.contracts import validator

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads((ROOT / "tests/fixtures/coordination/transaction-examples.json").read_text())


def _pending() -> dict[str, Any]:
    return copy.deepcopy(
        next(
            item
            for item in EXAMPLES["valid_results"]
            if item["operation"] == "create_runtime_intent"
            and item.get("value", {}).get("runtime", False) is None
        )
    )


@pytest.mark.parametrize("operation", ["create_runtime_intent", "query_outcome"])
@pytest.mark.parametrize("state", ["pending", "reconciling", "quarantined"])
def test_unobserved_intent_has_request_and_coordinator_time_not_fake_runtime(
    operation: str,
    state: str,
) -> None:
    result = _pending()
    result["operation"] = operation
    result["outcome"] = "replayed" if operation == "query_outcome" else "committed"
    result["value"]["intent_state"] = state
    validator("internal/coordination-transactions.schema.json", "RuntimeSnapshot").validate(
        result["value"]
    )
    validator("internal/coordination-transactions.schema.json", "TransactionResult").validate(
        result
    )
    assert validate_coordination("TransactionResult", result) is result


@pytest.mark.parametrize(
    "change", ["succeeded", "missing_request", "missing_time", "observed", "fake_state"]
)
def test_pending_intent_cannot_claim_successful_observation_or_omit_original(
    change: str,
) -> None:
    result = _pending()
    value = result["value"]
    if change == "succeeded":
        value["intent_state"] = "succeeded"
    elif change == "missing_request":
        del value["request"]
    elif change == "missing_time":
        del value["recorded_at_us"]
    elif change == "observed":
        result["operation"] = "observe_runtime"
    else:
        value["observed_instance_state"] = "absent"
    if change != "observed":
        assert not validator(
            "internal/coordination-transactions.schema.json", "RuntimeSnapshot"
        ).is_valid(value)
    assert not validator(
        "internal/coordination-transactions.schema.json", "TransactionResult"
    ).is_valid(result)
    with pytest.raises(ContractValidationError):
        validate_coordination("TransactionResult", result)


@pytest.mark.parametrize("example", EXAMPLES["invalid_results"], ids=lambda item: item["reason"])
def test_shared_consumers_reject_false_observation_and_completion(example: dict[str, Any]) -> None:
    result = example["result"]
    assert not validator(
        "internal/coordination-transactions.schema.json", "TransactionResult"
    ).is_valid(result)
    with pytest.raises(ContractValidationError):
        validate_coordination("TransactionResult", result)
