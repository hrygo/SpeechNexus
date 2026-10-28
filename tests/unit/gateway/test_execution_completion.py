"""A success transition needs the engine's own terminal fact and nothing weaker."""

from datetime import UTC, datetime, timedelta

import pytest

from speechnexus_gateway.application.execution_completion import ExecutionCompletionService
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.recovery import (
    TerminalStopEvidenceUnavailableError,
    engine_terminal_stop_evidence,
)
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionState,
    RecordStop,
    StopEvidenceKind,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    VerifiedStopEvidence,
)
from speechnexus_gateway.domain.terminal import EngineTerminalResponse
from tests.fake.execution import InMemorySubmissionLedger
from tests.fake.execution_command import original_request

_RESPONSE = "nexus-synthetic-terminal"


def executing(ledger: InMemorySubmissionLedger) -> SubmissionIntent:
    request = original_request()
    at = datetime.now(UTC)
    intent = SubmissionIntent(
        request.identity, at, at, request.request_digest, request.output_stream_id
    )
    ledger.add(intent, request_document=request.request_document)
    intent.mark_dispatched("file-asr-synthetic", at=at)
    return ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(intent, expected_version=1)
    )


def terminal(intent: SubmissionIntent, *, at: datetime | None = None) -> EngineTerminalResponse:
    return EngineTerminalResponse(
        intent.identity.submit_intent_id,
        (_RESPONSE,),
        at if at is not None else datetime.now(UTC),
    )


def test_success_records_the_engine_terminal_fact_and_releases_capacity() -> None:
    ledger = InMemorySubmissionLedger()
    service = ExecutionCompletionService(ledger=ledger)
    intent = executing(ledger)
    fact = terminal(intent)

    completed = service.complete(intent, fact)

    assert completed.execution_state is ExecutionState.SUCCEEDED
    assert completed.cleanup_state.value == "complete"
    assert completed.stop_evidence_kind is StopEvidenceKind.ENGINE_TERMINAL_RESPONSE
    assert completed.error_code is None
    assert not completed.execution_state.holds_capacity
    evidence = ledger._evidence[intent.identity.submit_intent_id]
    assert evidence.is_verified
    assert evidence.evidence_kind is StopEvidenceKind.ENGINE_TERMINAL_RESPONSE
    assert evidence.observed_at_us == int(fact.observed_at.timestamp() * 1_000_000)


@pytest.mark.parametrize("missing", ["absent", "other_execution", "before_dispatch"])
def test_untrusted_or_missing_facts_keep_capacity(missing: str) -> None:
    ledger = InMemorySubmissionLedger()
    service = ExecutionCompletionService(ledger=ledger)
    intent = executing(ledger)
    fact = (
        None
        if missing == "absent"
        else terminal(
            intent,
            at=(
                (intent.dispatched_at or datetime.now(UTC)) - timedelta(seconds=1)
                if missing == "before_dispatch"
                else None
            ),
        )
    )
    if missing == "other_execution":
        fact = EngineTerminalResponse("intent-another", (_RESPONSE,), datetime.now(UTC))

    with pytest.raises(TerminalStopEvidenceUnavailableError):
        service.complete(intent, fact)

    current = ledger.get(intent.identity.submit_intent_id)
    assert current is not None
    assert current.execution_state is ExecutionState.EXECUTING
    assert current.execution_state.holds_capacity
    assert not ledger._evidence


def test_replaying_the_same_terminal_fact_is_one_immutable_success() -> None:
    ledger = InMemorySubmissionLedger()
    service = ExecutionCompletionService(ledger=ledger)
    intent = executing(ledger)
    fact = terminal(intent)

    first = service.complete(intent, fact)
    replay = service.complete(intent, fact)

    assert replay == first
    assert len(ledger._evidence) == 1


def test_an_execution_closed_by_another_fact_is_never_reversed() -> None:
    ledger = InMemorySubmissionLedger()
    service = ExecutionCompletionService(ledger=ledger)
    intent = executing(ledger)
    stopped = ledger.record_stop(
        RecordStop(
            VerifiedStopEvidence(
                evidence_id="stop-instance-synthetic",
                submit_intent_id=intent.identity.submit_intent_id,
                evidence_kind=StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
                evidence_digest="sha256:" + "a" * 64,
                observed_at_us=int(datetime.now(UTC).timestamp() * 1_000_000),
                execution_stopped=True,
                resources_released=True,
                reason=None,
            ),
            ExecutionState.ABORTED,
        )
    )
    assert stopped.execution_state is ExecutionState.ABORTED

    assert service.complete(intent, terminal(intent)).execution_state is ExecutionState.ABORTED


def test_evidence_digest_binds_every_answered_request_identity() -> None:
    ledger = InMemorySubmissionLedger()
    intent = executing(ledger)
    first = engine_terminal_stop_evidence(intent, terminal(intent))
    second = engine_terminal_stop_evidence(
        intent,
        EngineTerminalResponse(
            intent.identity.submit_intent_id, (_RESPONSE, _RESPONSE + "-2"), datetime.now(UTC)
        ),
    )

    again = engine_terminal_stop_evidence(intent, terminal(intent))
    assert first is not None and second is not None and again is not None
    assert first.evidence_digest != second.evidence_digest
    assert first.evidence_id == again.evidence_id
    assert first.submit_intent_id == intent.identity.submit_intent_id


def test_an_execution_not_in_the_ledger_is_never_completed() -> None:
    service = ExecutionCompletionService(ledger=InMemorySubmissionLedger())
    request = original_request()
    at = datetime.now(UTC)
    absent = SubmissionIntent(
        request.identity, at, at, request.request_digest, request.output_stream_id
    )

    with pytest.raises(SubmissionOutcomeUnknownError):
        service.complete(
            absent,
            EngineTerminalResponse(
                absent.identity.submit_intent_id, (_RESPONSE,), datetime.now(UTC)
            ),
        )


def test_a_concurrent_stop_wins_while_this_writer_is_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemorySubmissionLedger()
    intent = executing(ledger)
    evidence = VerifiedStopEvidence(
        evidence_id="stop-instance-synthetic",
        submit_intent_id=intent.identity.submit_intent_id,
        evidence_kind=StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
        evidence_digest="sha256:" + "a" * 64,
        observed_at_us=int(datetime.now(UTC).timestamp() * 1_000_000),
        execution_stopped=True,
        resources_released=True,
        reason=None,
    )
    stop = ledger.record_stop

    def close_then_conflict(request: RecordStop) -> SubmissionIntent:
        stop(RecordStop(evidence, ExecutionState.ABORTED))
        raise ExecutionCleanupPendingError("another writer closed this execution")

    monkeypatch.setattr(ledger, "record_stop", close_then_conflict)

    completed = ExecutionCompletionService(ledger=ledger).complete(intent, terminal(intent))

    assert completed.execution_state is ExecutionState.ABORTED


def test_a_terminal_fact_needs_a_recorded_execution_identity() -> None:
    with pytest.raises(ValueError):
        EngineTerminalResponse("-unrecorded", (_RESPONSE,), datetime.now(UTC))


def test_a_terminal_fact_needs_distinct_request_identities() -> None:
    with pytest.raises(ValueError):
        EngineTerminalResponse("intent-a", (), datetime.now(UTC))
    with pytest.raises(ValueError):
        EngineTerminalResponse("intent-a", (_RESPONSE, _RESPONSE), datetime.now(UTC))


def test_a_terminal_fact_needs_an_aware_time() -> None:
    with pytest.raises(ValueError):
        EngineTerminalResponse("intent-a", (_RESPONSE,), datetime(2026, 1, 1))


def test_a_conflict_without_a_terminal_state_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemorySubmissionLedger()
    intent = executing(ledger)

    def still_pending(request: RecordStop) -> SubmissionIntent:
        raise ExecutionCleanupPendingError("cleanup is still pending")

    monkeypatch.setattr(ledger, "record_stop", still_pending)

    with pytest.raises(ExecutionCleanupPendingError):
        ExecutionCompletionService(ledger=ledger).complete(intent, terminal(intent))
