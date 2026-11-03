"""Original abort recovery is typed, optional and carries no execution authority."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.execution import parse


def status() -> dict[str, Any]:
    examples = json.loads(Path("tests/fixtures/contracts/consumer-profiles.json").read_text())[
        "examples"
    ]
    return copy.deepcopy(
        next(
            x["value"]
            for x in examples
            if x["profile"] == "execution"
            and x["schema"] == "ExecutionStatusResponse"
            and x["valid"]
        )
    )


def recovery(*, claimed: bool = False) -> dict[str, Any]:
    return {
        "execution_request_digest": "sha256:" + "3" * 64,
        "document_state": "verified",
        "receipt": {
            "abort_request_digest": "sha256:" + "4" * 64,
            "received_at": "2026-11-03T00:00:00.000000Z",
            "claim_state": "claimed" if claimed else "received",
            "claimed_at": "2026-11-03T00:00:01.000000Z" if claimed else None,
        },
    }


@pytest.mark.parametrize("claimed", [False, True])
def test_execution_get_accepts_typed_original_abort_recovery(claimed: bool) -> None:
    value = status()
    value["abort_recovery"] = recovery(claimed=claimed)
    parsed = parse("ExecutionRecoveryStatusResponse", value)
    assert parsed == value


def test_old_status_and_confirmed_no_abort_are_distinct() -> None:
    value = status()
    assert "abort_recovery" not in parse("ExecutionStatusResponse", value)
    with pytest.raises(ContractValidationError):
        parse("ExecutionRecoveryStatusResponse", value)
    value["abort_recovery"] = {**recovery(), "receipt": None}
    assert parse("ExecutionRecoveryStatusResponse", value)["abort_recovery"]["receipt"] is None


@pytest.mark.parametrize(
    "field,value",
    [("request_document", "private"), ("reason", "client_cancelled"), ("resources_released", True)],
)
def test_abort_recovery_cannot_carry_private_body_or_release_claims(
    field: str, value: object
) -> None:
    original = status()
    original["abort_recovery"] = recovery()
    original["abort_recovery"][field] = value
    with pytest.raises(ContractValidationError):
        parse("ExecutionRecoveryStatusResponse", original)


@pytest.mark.parametrize("claimed,at", [(False, "2026-11-03T00:00:01Z"), (True, None)])
def test_claim_state_and_timestamp_must_agree(claimed: bool, at: str | None) -> None:
    value = status()
    value["abort_recovery"] = recovery(claimed=claimed)
    value["abort_recovery"]["receipt"]["claimed_at"] = at
    with pytest.raises(ContractValidationError):
        parse("ExecutionRecoveryStatusResponse", value)


def test_redaction_does_not_claim_to_have_reverified_the_body() -> None:
    value = status()
    value["abort_recovery"] = {**recovery(), "document_state": "redacted"}
    assert (
        parse("ExecutionRecoveryStatusResponse", value)["abort_recovery"]["document_state"]
        == "redacted"
    )
