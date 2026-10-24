"""Named full results for the submit-intent lifecycle, without an engine model."""

from __future__ import annotations

from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.recovery import observed_at_microseconds
from speechnexus_gateway.domain.submission import VerifiedStopEvidence
from speechnexus_gateway.interfaces.submit_transactions import ExecutionFactBoundary
from tests.unit.gateway.test_submit_execution import NOW, build_service
from tests.unit.gateway.test_submit_http import (
    OTHER_COMMAND,
    evidence_for,
    submit_body,
)


def stop_request(
    evidence: VerifiedStopEvidence,
    *,
    terminal_state: str = "aborted",
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "evidence": {
            "evidence_id": evidence.evidence_id,
            "submit_intent_id": evidence.submit_intent_id,
            "evidence_kind": evidence.evidence_kind.value,
            "evidence_digest": evidence.evidence_digest,
            "observed_at_us": evidence.observed_at_us,
            "execution_stopped": evidence.execution_stopped,
            "resources_released": evidence.resources_released,
            "reason": evidence.reason,
        },
        "terminal_state": terminal_state,
        "error_code": error_code,
    }


def unknown_request(submit_intent_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "submit_intent_id": submit_intent_id,
        "reason_code": "engine_transport_unknown",
        "observed_at_us": observed_at_microseconds(NOW),
    } | changes


def create_submit_request(**changes: Any) -> dict[str, Any]:
    return {"execution_request": submit_body(**changes), "output_stream_id": None}


@pytest.fixture
def boundary() -> tuple[ExecutionFactBoundary, Any, Any]:
    service, ledger, engine = build_service()
    return ExecutionFactBoundary(submit=service, ledger=ledger), ledger, engine


def test_create_submit_reports_committed_then_replayed_without_a_second_dispatch(
    boundary: tuple[ExecutionFactBoundary, Any, Any],
) -> None:
    facts, ledger, engine = boundary
    request = create_submit_request()

    first = facts.create_submit(request)
    replay = facts.create_submit(request)

    assert first["outcome"] == "committed"
    assert first["scope_id"] == "intent-1"
    assert first["recovery"] == "none"
    assert first["version"] == "2"
    assert first["value"]["kind"] == "execution"
    assert first["value"]["execution"]["execution_state"] == "executing"
    assert replay["outcome"] == "replayed"
    assert replay["value"] == first["value"]
    assert engine.dispatch_count == 1
    stored = ledger.get("intent-1")
    assert stored is not None and stored.version == 2


def test_create_submit_reports_another_command_as_rejected_without_a_second_dispatch(
    boundary: tuple[ExecutionFactBoundary, Any, Any],
) -> None:
    facts, _, engine = boundary
    assert facts.create_submit(create_submit_request())["outcome"] == "committed"

    conflict = facts.create_submit(
        {"execution_request": submit_body(command=OTHER_COMMAND), "output_stream_id": None}
    )

    assert conflict["outcome"] == "rejected"
    assert conflict["error_code"] == "submit_intent_conflict"
    assert conflict["recovery"] == "none"
    assert "value" not in conflict
    assert engine.dispatch_count == 1


def test_a_digest_that_does_not_describe_the_command_is_rejected_before_dispatch(
    boundary: tuple[ExecutionFactBoundary, Any, Any],
) -> None:
    facts, ledger, engine = boundary
    request = {
        "execution_request": submit_body(digest=command_digest(OTHER_COMMAND)),
        "output_stream_id": None,
    }

    rejected = facts.create_submit(request)

    assert rejected["outcome"] == "rejected"
    assert rejected["error_code"] == "invalid_execution_request"
    assert engine.dispatch_count == 0
    assert ledger.get("intent-1") is None


def test_a_pcm_stage_records_the_stream_the_command_names(
    boundary: tuple[ExecutionFactBoundary, Any, Any],
) -> None:
    facts, ledger, _ = boundary
    request = {
        "execution_request": submit_body(max_output_audio_ms=1000, sample_rate_hz=24000),
        "output_stream_id": "stream-stage-1",
    }

    committed = facts.create_submit(request)

    assert committed["outcome"] == "committed"
    stored = ledger.get("intent-1")
    assert stored is not None and stored.output_stream_id == "stream-stage-1"
    assert stored.identity.sample_rate_hz == 24000


def test_record_stop_reports_committed_then_replayed_with_the_same_fact(
    boundary: tuple[ExecutionFactBoundary, Any, Any],
) -> None:
    facts, ledger, engine = boundary
    assert facts.create_submit(create_submit_request())["outcome"] == "committed"
    request = stop_request(evidence_for())

    first = facts.record_stop(request)
    replay = facts.record_stop(request)

    assert first["outcome"] == "committed"
    assert first["version"] == "3"
    assert first["value"]["execution"]["execution_state"] == "aborted"
    assert first["value"]["execution"]["cleanup_state"] == "complete"
    assert replay["outcome"] == "replayed"
    assert replay["version"] == first["version"]
    assert replay["value"] == first["value"]
    stored = ledger.get("intent-1")
    assert stored is not None and stored.version == 3
    # A stop never dispatches and never releases a budget this boundary does not own.
    assert engine.dispatch_count == 1


def test_record_stop_refuses_unconfirmed_evidence_at_the_frozen_boundary(
    boundary: tuple[ExecutionFactBoundary, Any, Any],
) -> None:
    facts, ledger, _ = boundary
    assert facts.create_submit(create_submit_request())["outcome"] == "committed"
    unconfirmed = stop_request(evidence_for())
    unconfirmed["evidence"] = {**unconfirmed["evidence"], "resources_released": False}

    with pytest.raises(ContractValidationError):
        facts.record_stop(unconfirmed)

    stored = ledger.get("intent-1")
    assert stored is not None and stored.execution_state.value == "executing"


def test_mark_execution_unknown_reports_committed_then_replayed(
    boundary: tuple[ExecutionFactBoundary, Any, Any],
) -> None:
    facts, ledger, _ = boundary
    assert facts.create_submit(create_submit_request())["outcome"] == "committed"
    request = unknown_request("intent-1")

    first = facts.mark_execution_unknown(request)
    replay = facts.mark_execution_unknown(request)

    assert first["outcome"] == "committed"
    assert first["version"] == "3"
    assert first["value"]["execution"]["execution_state"] == "unknown_quarantined"
    assert first["value"]["execution"]["cleanup_state"] == "quarantined"
    assert replay["outcome"] == "replayed"
    assert replay["value"] == first["value"]
    stored = ledger.get("intent-1")
    assert stored is not None and stored.version == 3

    another_reason = facts.mark_execution_unknown(
        unknown_request("intent-1", reason_code="cancellation_unconfirmed")
    )
    assert another_reason["outcome"] == "rejected"
    assert another_reason["error_code"] == "idempotency_conflict"
    assert another_reason["recovery"] == "query_original"


def test_a_terminal_execution_is_not_downgraded_to_unknown(
    boundary: tuple[ExecutionFactBoundary, Any, Any],
) -> None:
    facts, _, _ = boundary
    assert facts.create_submit(create_submit_request())["outcome"] == "committed"
    assert facts.record_stop(stop_request(evidence_for()))["outcome"] == "committed"

    refused = facts.mark_execution_unknown(unknown_request("intent-1"))

    assert refused["outcome"] == "rejected"
    assert refused["error_code"] == "execution_cleanup_pending"
    assert refused["recovery"] == "reconcile"


def test_a_missing_execution_is_reported_as_rejected_not_as_a_new_fact(
    boundary: tuple[ExecutionFactBoundary, Any, Any],
) -> None:
    facts, ledger, engine = boundary

    missing = facts.mark_execution_unknown(unknown_request("intent-absent"))

    assert missing["outcome"] == "rejected"
    assert missing["error_code"] == "invalid_execution_request"
    assert ledger.get("intent-absent") is None
    assert engine.dispatch_count == 0


@pytest.mark.parametrize("operation", ["create_submit", "record_stop", "mark_execution_unknown"])
def test_every_command_rejects_an_unknown_field(
    boundary: tuple[ExecutionFactBoundary, Any, Any], operation: str
) -> None:
    facts, ledger, _ = boundary
    if operation == "create_submit":
        request = create_submit_request() | {"operator": "synthetic"}
    elif operation == "record_stop":
        request = stop_request(evidence_for()) | {"operator": "synthetic"}
    else:
        request = unknown_request("intent-1") | {"operator": "synthetic"}

    with pytest.raises(ContractValidationError):
        getattr(facts, operation)(request)

    assert ledger.get("intent-1") is None
