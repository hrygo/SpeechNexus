"""A completed stage that dispatched nothing closes as success, with its own proof.

`execution_cleanup_pending`/`unknown_quarantined` used to be the only exit for an ASR
execution the VAD gated to zero engine calls: the stage had stayed entirely local, but
the completion path required an engine terminal answer that could never exist, so the
lease was held forever and the next request answered `capacity_exhausted`. The stage now
attests its own dispatch count, and `undispatched_stop_evidence` is the single place that
turns "exactly zero" into release evidence.

These tests pin that convergence, the shape it must keep (a dispatched run with a missing
answer still holds capacity), and the replay rule (one fact per submit intent).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from speechnexus_gateway.application.execution_completion import ExecutionCompletionService
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.recovery import (
    TerminalStopEvidenceUnavailableError,
    engine_terminal_stop_evidence,
    undispatched_stop_evidence,
)
from speechnexus_gateway.domain.submission import (
    ExecutionState,
    InvalidExecutionRequestError,
    RecordStop,
    StopEvidenceKind,
    SubmissionIntent,
    VerifiedStopEvidence,
)
from speechnexus_gateway.domain.terminal import DispatchAttestation
from tests.fake.execution import InMemorySubmissionLedger
from tests.fake.execution_command import original_request

#: The measured gap between dispatch and the failed completion in the live samples: four
#: executions died 45.7 ms to 73 ms after dispatch with a result already staged.
OBSERVED_GAP = timedelta(milliseconds=46)
#: The dispatcher's own lead time, so the ledger clock is always after the stage's instant
#: (in production the completion is recorded after the stage returns, never before).
LEAD = timedelta(milliseconds=500)


def executing(ledger: InMemorySubmissionLedger, *, at: datetime) -> SubmissionIntent:
    request = original_request()
    intent = SubmissionIntent(
        request.identity, at, at, request.request_digest, request.output_stream_id
    )
    ledger.add(intent, request_document=request.request_document)
    intent.mark_dispatched("file-asr-synthetic", at=at)
    return ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(intent, expected_version=1)
    )


def no_dispatch(at: datetime) -> DispatchAttestation:
    return DispatchAttestation(0, 0, at)


def test_a_stage_that_dispatched_nothing_completes_successfully() -> None:
    ledger = InMemorySubmissionLedger()
    service = ExecutionCompletionService(ledger=ledger)
    at = datetime.now(UTC) - LEAD
    intent = executing(ledger, at=at)

    completed = service.complete(intent, None, dispatch=no_dispatch(at + OBSERVED_GAP))

    assert completed.execution_state is ExecutionState.SUCCEEDED
    assert completed.cleanup_state.value == "complete"
    assert completed.stop_evidence_kind is StopEvidenceKind.NOT_DISPATCHED
    assert completed.error_code is None
    assert completed.terminated_at is not None
    assert not completed.execution_state.holds_capacity
    evidence = ledger._evidence[intent.identity.submit_intent_id]
    assert evidence.evidence_kind is StopEvidenceKind.NOT_DISPATCHED
    assert evidence.execution_stopped and evidence.resources_released


def test_the_observed_forty_six_millisecond_signature_converges() -> None:
    """The live refusal signature: dispatch, staged result, then a 46 ms completion."""
    ledger = InMemorySubmissionLedger()
    service = ExecutionCompletionService(ledger=ledger)
    dispatched_at = datetime.now(UTC) - LEAD
    intent = executing(ledger, at=dispatched_at)
    attested_at = dispatched_at + OBSERVED_GAP

    completed = service.complete(intent, None, dispatch=no_dispatch(attested_at))

    assert completed.execution_state is ExecutionState.SUCCEEDED
    assert completed.cleanup_state.value == "complete"
    evidence = ledger._evidence[intent.identity.submit_intent_id]
    # The proof names the instant the stage finished, not the dispatch instant.
    assert evidence.observed_at_us == int(attested_at.timestamp() * 1_000_000)
    assert completed.updated_at >= dispatched_at + OBSERVED_GAP


def test_the_undispatched_proof_binds_the_identity_the_zero_count_and_the_instant() -> None:
    ledger = InMemorySubmissionLedger()
    at = datetime.now(UTC) - LEAD
    intent = executing(ledger, at=at)

    first = undispatched_stop_evidence(intent, no_dispatch(at + OBSERVED_GAP))
    later = undispatched_stop_evidence(intent, no_dispatch(at + timedelta(seconds=1)))

    assert first is not None and later is not None
    assert first.evidence_kind is StopEvidenceKind.NOT_DISPATCHED
    assert first.submit_intent_id == intent.identity.submit_intent_id
    assert first.evidence_digest != later.evidence_digest  # the instant is bound
    assert first.evidence_id == later.evidence_id  # one identity, one evidence row
    # It carries no engine answer: this path never claims an engine said anything.
    assert engine_terminal_stop_evidence(intent, None) is None


@pytest.mark.parametrize("dispatched", [1, 2])
def test_a_dispatched_run_without_an_answer_keeps_its_capacity(dispatched: int) -> None:
    ledger = InMemorySubmissionLedger()
    service = ExecutionCompletionService(ledger=ledger)
    at = datetime.now(UTC) - LEAD
    intent = executing(ledger, at=at)

    with pytest.raises(TerminalStopEvidenceUnavailableError):
        service.complete(
            intent, None, dispatch=DispatchAttestation(dispatched, 0, at + OBSERVED_GAP)
        )

    current = ledger.get(intent.identity.submit_intent_id)
    assert current is not None
    assert current.execution_state is ExecutionState.EXECUTING
    assert current.execution_state.holds_capacity
    assert not ledger._evidence


def test_a_completion_without_an_attestation_keeps_its_capacity() -> None:
    """Without the stage's own record there is no proof, so the old refusal stands."""
    ledger = InMemorySubmissionLedger()
    service = ExecutionCompletionService(ledger=ledger)
    at = datetime.now(UTC) - LEAD
    intent = executing(ledger, at=at)

    with pytest.raises(TerminalStopEvidenceUnavailableError):
        service.complete(intent, None)

    assert not ledger._evidence


def test_replaying_the_undispatched_completion_is_one_immutable_fact() -> None:
    ledger = InMemorySubmissionLedger()
    service = ExecutionCompletionService(ledger=ledger)
    at = datetime.now(UTC) - LEAD
    intent = executing(ledger, at=at)
    attestation = no_dispatch(at + OBSERVED_GAP)

    first = service.complete(intent, None, dispatch=attestation)
    replay = service.complete(intent, None, dispatch=attestation)

    assert replay == first
    assert len(ledger._evidence) == 1
    assert first.stop_evidence_kind is StopEvidenceKind.NOT_DISPATCHED


def test_success_still_rejects_a_proof_that_is_not_a_success_proof() -> None:
    """The new kind widens one branch; it does not let any kind close a success."""
    at = datetime.now(UTC)
    evidence = VerifiedStopEvidence(
        evidence_id="stop-instance-synthetic",
        submit_intent_id="submit-synthetic",
        evidence_kind=StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
        evidence_digest="sha256:" + "c" * 64,
        observed_at_us=int(at.timestamp() * 1_000_000),
        execution_stopped=True,
        resources_released=True,
        reason=None,
    )

    with pytest.raises(InvalidExecutionRequestError, match="success requires a terminal or"):
        RecordStop(evidence, ExecutionState.SUCCEEDED)
    # The same evidence still closes an abort: only the success branch was widened.
    assert RecordStop(evidence, ExecutionState.ABORTED).terminal_state is ExecutionState.ABORTED
