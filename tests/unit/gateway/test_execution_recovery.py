"""Unknown recovery preserves facts; reads never dispatch, reconcile or rewrite."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts.execution import parse
from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.recovery import (
    ExecutionObservationConflictError,
    ExecutionSnapshot,
    MarkExecutionUnknown,
)
from speechnexus_gateway.domain.submission import (
    CancellationUnconfirmedError,
    CleanupState,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    RecordStop,
    StopEvidenceKind,
    SubmissionIntent,
    SubmissionStoreError,
    SubmissionStoreUnavailableError,
    SubmitRequest,
)
from tests.fake.execution import InMemorySubmissionLedger
from tests.unit.gateway.test_barrier_http import HEADERS, app_for
from tests.unit.gateway.test_submit_execution import COMMAND, NOW, identity_for, stop_evidence

OBSERVED = int(NOW.timestamp()) * 1_000_000
UNKNOWN = MarkExecutionUnknown("intent-1", ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, OBSERVED)


def pending(ledger: InMemorySubmissionLedger, **identity_changes: object) -> SubmissionIntent:
    request = SubmitRequest(identity_for(COMMAND, **identity_changes), COMMAND)
    intent = SubmissionIntent(
        identity=request.identity,
        recorded_at=NOW,
        updated_at=NOW,
        request_digest=request.request_digest,
        output_stream_id=request.output_stream_id,
    )
    ledger.add(intent, request_document=request.request_document)
    return intent


def test_unknown_keeps_unobserved_dispatch_empty_and_replays_the_original_fact() -> None:
    clock = [NOW]
    ledger = InMemorySubmissionLedger(now=lambda: clock[0])
    original = pending(ledger)
    clock[0] += timedelta(seconds=1)
    unknown = ledger.mark_execution_unknown(UNKNOWN)
    assert unknown.dispatched_at is None and unknown.engine_request_id is None
    assert unknown.identity == original.identity
    assert unknown.request_digest == original.request_digest
    assert unknown.execution_state.holds_capacity
    assert unknown.version == 2
    clock[0] += timedelta(seconds=1)
    assert ledger.mark_execution_unknown(UNKNOWN) == unknown
    with pytest.raises(ExecutionObservationConflictError):
        ledger.mark_execution_unknown(
            replace(UNKNOWN, reason_code=ExecutionErrorCode.CANCELLATION_UNCONFIRMED)
        )
    snapshot = ledger.query_outcome("intent-1")
    assert snapshot is not None and snapshot.intent == unknown
    assert snapshot.status()["stop_evidence_kind"] == "unconfirmed_timeout"
    parse("ExecutionStatusResponse", snapshot.status())


def test_a_stale_unknown_observation_does_not_replace_a_newer_executing_fact() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW + timedelta(seconds=3))
    intent = pending(ledger)
    intent.mark_dispatched("engine-request-1", at=NOW + timedelta(seconds=2))
    ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(intent, expected_version=1)
    )
    with pytest.raises(InvalidExecutionRequestError):
        ledger.mark_execution_unknown(UNKNOWN)
    known = replace(UNKNOWN, observed_at_us=OBSERVED + 3_000_000)
    unknown = ledger.mark_execution_unknown(known)
    assert unknown.dispatched_at == NOW + timedelta(seconds=2)
    assert unknown.engine_request_id == "engine-request-1"


@pytest.mark.parametrize(
    "kind", [StopEvidenceKind.ENGINE_ABORT_ACK, StopEvidenceKind.ENGINE_TERMINAL_RESPONSE]
)
def test_unobserved_dispatch_cannot_be_closed_with_a_request_ack(kind: StopEvidenceKind) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    pending(ledger)
    ledger.mark_execution_unknown(UNKNOWN)
    with pytest.raises(ExecutionCleanupPendingError):
        ledger.record_stop(RecordStop(stop_evidence(kind=kind), ExecutionState.ABORTED))


@pytest.mark.parametrize(
    "kind",
    [
        StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
        StopEvidenceKind.OPERATOR_VERIFIED_INSTANCE_TERMINATED,
    ],
)
def test_verified_instance_stop_closes_unknown_without_inventing_dispatch(
    kind: StopEvidenceKind,
) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    pending(ledger)
    ledger.mark_execution_unknown(UNKNOWN)
    evidence = stop_evidence(kind=kind)
    closed = ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED))
    assert closed.dispatched_at is None and closed.engine_request_id is None
    assert closed.terminated_at == NOW and closed.version == 3
    assert ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED)) == closed
    parse("ExecutionStatusResponse", ExecutionSnapshot(closed).status())
    with pytest.raises(ExecutionCleanupPendingError):
        ledger.mark_execution_unknown(UNKNOWN)


def test_instance_stop_still_requires_both_confirmations_and_an_unknown_transition() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    pending(ledger)
    evidence = stop_evidence(kind=StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER)
    with pytest.raises(ExecutionCleanupPendingError):
        ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED))
    ledger.mark_execution_unknown(UNKNOWN)
    for incomplete in (
        replace(evidence, execution_stopped=False),
        replace(evidence, resources_released=False),
    ):
        with pytest.raises(CancellationUnconfirmedError):
            ledger.record_stop(RecordStop(incomplete, ExecutionState.ABORTED))
    retained = ledger.get("intent-1")
    assert retained is not None and retained.execution_state is ExecutionState.UNKNOWN_QUARANTINED


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_at_us": -1},
        {"observed_at_us": True},
        {"observed_at_us": 2**53},
        {"submit_intent_id": "BAD ID"},
        {"reason_code": "made_up"},
    ],
)
def test_unknown_request_enforces_the_frozen_scalar_domains(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        replace(UNKNOWN, **changes)  # type: ignore[arg-type]


def test_unknown_matches_the_packaged_coordination_request() -> None:
    validate_coordination(
        "mark_execution_unknown",
        {
            "submit_intent_id": UNKNOWN.submit_intent_id,
            "reason_code": UNKNOWN.reason_code.value,
            "observed_at_us": UNKNOWN.observed_at_us,
        },
    )


def test_generated_output_cannot_be_reinterpreted_as_an_unsent_failure() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    intent = pending(ledger)
    with pytest.raises(ValueError, match="no generated output"):
        replace(
            intent,
            identity=replace(intent.identity, max_output_audio_ms=2000, sample_rate_hz=24000),
            output_stream_id="stream-1",
            execution_state=ExecutionState.FAILED,
            cleanup_state=CleanupState.NOT_REQUIRED,
            error_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
            generated_pcm_samples=12000,
        )


async def test_http_query_is_read_only_and_scoped_to_the_gateway(tmp_path: Path) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    pending(ledger)
    pending(ledger, submit_intent_id="intent-foreign", gateway_id="gateway-other")
    ledger.mark_execution_unknown(UNKNOWN)
    before = ledger.get("intent-1")
    app, _ = app_for(tmp_path, ledger=ledger)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/internal/v1/executions/intent-1", headers=HEADERS)
        repeated = await client.get("/internal/v1/executions/intent-1", headers=HEADERS)
        foreign = await client.get("/internal/v1/executions/intent-foreign", headers=HEADERS)
        unauthorized = await client.get("/internal/v1/executions/intent-1")
    assert response.status_code == repeated.status_code == 200
    assert response.json() == repeated.json()
    assert response.json()["dispatched_at"] is None
    assert response.json()["engine_request_id"] is None
    assert "request_document" not in response.text and "staging_artifact_ref" not in response.json()
    parse("ExecutionStatusResponse", response.json())
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "control_execution_not_found"
    assert unauthorized.status_code == 401
    assert ledger.get("intent-1") == before


@pytest.mark.parametrize(
    "error,code,recovery",
    [
        (SubmissionStoreUnavailableError(), "control_store_unavailable", "retry_transaction"),
        (SubmissionStoreError(), "control_reconciliation_required", "reconcile"),
    ],
)
async def test_http_query_failure_never_reports_not_found_or_echoes_store_details(
    tmp_path: Path, error: SubmissionStoreError, code: str, recovery: str
) -> None:
    class FailedLedger(InMemorySubmissionLedger):
        def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None:
            raise error

    app, _ = app_for(tmp_path, ledger=FailedLedger())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/internal/v1/executions/intent-1", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["recovery"] == recovery
    parse("ExecutionControlErrorResponse", response.json())


@pytest.mark.parametrize(
    "identifier,request_id",
    [("UPPER", "synthetic"), ("ab", "synthetic"), ("a" * 129, "synthetic"), ("intent-1", "")],
)
async def test_query_path_and_header_constraints_are_enforced(
    tmp_path: Path, identifier: str, request_id: str
) -> None:
    app, _ = app_for(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/internal/v1/executions/{identifier}",
            headers=HEADERS | {"X-Request-ID": request_id},
        )
    assert response.status_code == 422
    parse("ExecutionControlErrorResponse", response.json())
