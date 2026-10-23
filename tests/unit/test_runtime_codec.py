"""Complete runtime command persistence and public internal projection boundaries."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.runtime_intents import RuntimeService
from speechnexus_backend.speech.domain.runtime_intents import (
    RuntimeDeadline,
    RuntimeIdentity,
    RuntimeRecord,
    RuntimeResult,
    unknown,
)
from speechnexus_backend.speech.infrastructure.runtime_codec import (
    decode_record,
    encode_record,
    prepare_request,
    request_from_wire,
    request_to_wire,
    validate_original_observation,
)
from speechnexus_backend.speech.interfaces.runtime_transactions import RuntimeTransactionBoundary
from tests.fake.runtime_intents import NOW, PLAN, REQUEST


def test_complete_wire_and_approved_plan_survive_private_record_roundtrip() -> None:
    prepared = prepare_request(REQUEST)
    document = encode_record(prepared, PLAN, "scheduler-test")
    restored, plan, creator = decode_record(document)
    assert restored == prepared and plan == PLAN and creator == "scheduler-test"
    assert json.loads(document)["request"]["deadline_at"] == REQUEST.deadline.text
    assert request_from_wire(request_to_wire(REQUEST)) == REQUEST
    for change in (
        replace(REQUEST, fencing_token=8),
        replace(REQUEST, deadline=RuntimeDeadline("2250-01-01T00:00:00.123456788Z")),
        replace(REQUEST, binding=replace(REQUEST.binding, manifest_id="manifest-other")),
    ):
        other = prepare_request(change)
        assert other.request_digest != prepared.request_digest
        assert other.event_id == prepared.event_id
    assert (
        prepare_request(
            replace(REQUEST, binding=replace(REQUEST.binding, target_node_id="host-other"))
        ).event_id
        != prepared.event_id
    )


@pytest.mark.parametrize(
    "kind",
    [
        "legacy",
        "version",
        "missing_plan",
        "short_vector",
        "float_vector",
        "wrong_memory",
        "extra_field",
    ],
)
def test_old_missing_or_corrupt_records_require_reconciliation(kind: str) -> None:
    document = json.loads(encode_record(prepare_request(REQUEST), PLAN, "scheduler-test"))
    if kind == "legacy":
        document = request_to_wire(REQUEST)
    elif kind == "version":
        document["record_version"] = True
    elif kind == "missing_plan":
        del document["plan"]["qualification_digest"]
    elif kind == "short_vector":
        document["plan"]["peak"] = [20]
    elif kind == "float_vector":
        document["plan"]["peak"][0] = 20.0
    elif kind == "wrong_memory":
        document["plan"]["peak"][0] = 21
    else:
        document["unexpected"] = 1
    with pytest.raises(ValueError):
        decode_record(json.dumps(document))


class FixedRuntimePort:
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.calls = 0
        self.ambiguous = ambiguous
        self.record = RuntimeRecord(
            REQUEST,
            PLAN,
            "scheduler-test",
            prepare_request(REQUEST).request_digest,
            "pending",
            NOW,
            NOW,
            1,
        )

    def create_runtime_intent(self, command: Any) -> RuntimeResult:
        self.calls += 1
        assert command.owner_id == "scheduler-test"
        return (
            unknown(command.request.identity)
            if self.ambiguous
            else RuntimeResult("committed", REQUEST.identity, self.record)
        )

    def query_runtime_intent(
        self, identity: RuntimeIdentity, *, request_digest: str | None = None
    ) -> RuntimeResult:
        return RuntimeResult("replayed", identity, self.record)


def _boundary(port: FixedRuntimePort) -> RuntimeTransactionBoundary:
    return RuntimeTransactionBoundary(
        RuntimeService(port),
        owner_id="scheduler-test",
        decode_request=request_from_wire,
        render_request=request_to_wire,
        decode_observation=validate_original_observation,
    )


def test_boundary_preserves_input_and_publishes_only_the_unobserved_request() -> None:
    port = FixedRuntimePort()
    boundary = _boundary(port)
    request: dict[str, Any] = {"request": request_to_wire(REQUEST), "expected_head_version": None}
    request["request"]["instance_generation"] = 1.0
    original = copy.deepcopy(request)
    result = boundary.create_runtime_intent(request, approved_plan=PLAN)
    assert request == original and type(request["request"]["instance_generation"]) is float
    assert result["value"]["runtime"] is None
    assert type(result["value"]["request"]["instance_generation"]) is int
    assert {"plan", "creator_id", "qualification_digest"}.isdisjoint(result["value"])
    assert (
        boundary.query_outcome(
            {
                "target": {
                    "kind": "runtime_intent",
                    "target_node_id": "host-test",
                    "intent_id": "runtime-new",
                }
            }
        )["outcome"]
        == "replayed"
    )
    with pytest.raises(ContractValidationError):
        boundary.query_outcome({"target": {"kind": "pool", "pool_id": "tts-realtime"}})
    assert port.calls == 1


def test_unknown_creation_is_not_automatically_resubmitted_by_the_service_or_boundary() -> None:
    port = FixedRuntimePort(ambiguous=True)
    result = _boundary(port).create_runtime_intent(
        {"request": request_to_wire(REQUEST), "expected_head_version": None}, approved_plan=PLAN
    )
    assert result["outcome"] == "unknown" and result["recovery"] == "query_original"
    assert "value" not in result and port.calls == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("instance_generation", True),
        ("image", "unapproved.test/image"),
        ("owner_id", "caller-chosen"),
    ],
)
def test_wire_cannot_supply_coerced_counters_or_server_authority(key: str, value: Any) -> None:
    port = FixedRuntimePort()
    request = request_to_wire(REQUEST)
    request[key] = value
    with pytest.raises(ContractValidationError):
        _boundary(port).create_runtime_intent(
            {"request": request, "expected_head_version": None}, approved_plan=PLAN
        )
    assert port.calls == 0
