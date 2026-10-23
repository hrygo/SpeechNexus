"""Conditional observations never dispatch, publish output or attest a verified stop."""

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime

from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    StaleSubmissionVersionError,
    StopEvidenceKind,
    SubmissionIdentity,
    SubmissionIntent,
    contract_instant_text,
)


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    identity: SubmissionIdentity
    output_stream_id: str | None
    generated_pcm_samples: int
    execution_state: ExecutionState
    cleanup_state: CleanupState
    dispatched_at: datetime | None
    engine_request_id: str | None
    error_code: ExecutionErrorCode | None
    stop_evidence_kind: StopEvidenceKind
    observed_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.execution_state, ExecutionState)
            or not isinstance(self.cleanup_state, CleanupState)
            or not isinstance(self.stop_evidence_kind, StopEvidenceKind)
            or (self.error_code is not None and not isinstance(self.error_code, ExecutionErrorCode))
            or type(self.generated_pcm_samples) is not int
            or self.generated_pcm_samples < 0
        ):
            raise InvalidExecutionRequestError("observation uses invalid execution values")

    @classmethod
    def from_intent(cls, intent: SubmissionIntent) -> ExecutionObservation:
        return cls(
            intent.identity,
            intent.output_stream_id,
            intent.generated_pcm_samples,
            intent.execution_state,
            intent.cleanup_state,
            intent.dispatched_at,
            intent.engine_request_id,
            intent.error_code,
            (
                StopEvidenceKind.UNCONFIRMED_TIMEOUT
                if intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
                else intent.stop_evidence_kind
            ),
            intent.updated_at,
        )


@dataclass(frozen=True, slots=True)
class RecordExecutionObservation:
    observation: ExecutionObservation
    expected_version: int

    def __post_init__(self) -> None:
        if type(self.expected_version) is not int or not 1 <= self.expected_version < 2**63:
            raise InvalidExecutionRequestError("observation needs a stored signed-64-bit version")
        contract_instant_text(self.observation.observed_at)
        if self.observation.dispatched_at is not None:
            contract_instant_text(self.observation.dispatched_at)

    @classmethod
    def from_intent(
        cls, intent: SubmissionIntent, *, expected_version: int
    ) -> RecordExecutionObservation:
        return cls(ExecutionObservation.from_intent(intent), expected_version)

    def apply(self, current: SubmissionIntent, *, at: datetime) -> SubmissionIntent:
        observed = self.observation
        if current.version != self.expected_version:
            raise StaleSubmissionVersionError("execution changed before its observation")
        if (
            observed.identity != current.identity
            or observed.output_stream_id != current.output_stream_id
            or observed.generated_pcm_samples != current.generated_pcm_samples
        ):
            raise InvalidExecutionRequestError(
                "observation changes frozen identity or output facts"
            )
        if observed.observed_at > at or observed.observed_at < current.updated_at:
            raise InvalidExecutionRequestError("observation contradicts the recorded time")
        dispatch = observed.dispatched_at
        if dispatch is not None and (
            dispatch < current.recorded_at or dispatch > observed.observed_at
        ):
            raise InvalidExecutionRequestError("dispatch time contradicts execution history")
        previous = ExecutionObservation.from_intent(current)
        # An unchanged fact at the current version is a no-op, not a heartbeat write.
        if previous == replace(observed, observed_at=previous.observed_at):
            return current
        if current.execution_state.is_terminal or observed.cleanup_state is CleanupState.COMPLETE:
            raise ExecutionCleanupPendingError("terminal facts require record_stop")
        if (current.dispatched_at is not None and current.dispatched_at != dispatch) or (
            current.engine_request_id is not None
            and current.engine_request_id != observed.engine_request_id
        ):
            raise InvalidExecutionRequestError("observed dispatch association is immutable")
        if observed.execution_state is ExecutionState.SUBMITTING:
            raise InvalidExecutionRequestError("an observation cannot reset a submission")
        if (
            current.execution_state is ExecutionState.STOPPING
            and observed.execution_state is ExecutionState.EXECUTING
        ):
            raise ExecutionCleanupPendingError("a late executing status cannot undo stopping")
        if observed.cleanup_state is CleanupState.NOT_REQUIRED and (
            current.execution_state is not ExecutionState.SUBMITTING
            or current.dispatched_at is not None
            or current.engine_request_id is not None
            or current.generated_pcm_samples != 0
            or current.has_staging_output
        ):
            raise ExecutionCleanupPendingError("no-dispatch cleanup contradicts stored facts")
        if observed.execution_state is ExecutionState.UNKNOWN_QUARANTINED:
            if observed.stop_evidence_kind is not StopEvidenceKind.UNCONFIRMED_TIMEOUT:
                raise InvalidExecutionRequestError("unknown observation has no verified stop")
        elif observed.stop_evidence_kind is not StopEvidenceKind.NONE:
            raise ExecutionCleanupPendingError("an observation cannot manufacture stop evidence")
        if observed.execution_state in {ExecutionState.EXECUTING, ExecutionState.STOPPING} and (
            observed.error_code is not None
        ):
            raise InvalidExecutionRequestError("active execution does not carry an execution error")
        if current.version >= 2**63 - 1:
            raise ExecutionCleanupPendingError("execution version is exhausted")
        updated = deepcopy(current)
        updated.execution_state = observed.execution_state
        updated.cleanup_state = observed.cleanup_state
        updated.dispatched_at = dispatch
        updated.engine_request_id = observed.engine_request_id
        updated.error_code = observed.error_code
        try:
            updated._touch(at)
        except ValueError:
            raise InvalidExecutionRequestError(
                "observation violates the execution matrix"
            ) from None
        return updated
