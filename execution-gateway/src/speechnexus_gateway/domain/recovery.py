"""Uncertainty transitions and read-only projections; neither may issue engine work."""

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    SubmissionIntent,
    SubmissionStoreError,
    contract_instant_text,
    hard_output_ceiling_samples,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def observed_at_microseconds(value: datetime) -> int:
    """A contract instant as microseconds since the epoch, as transactions carry it."""
    if value.tzinfo is None:
        raise InvalidExecutionRequestError("observation instants must be timezone aware")
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


class ExecutionObservationConflictError(SubmissionStoreError):
    code = "idempotency_conflict"
    recovery = "query_original"


@dataclass(frozen=True, slots=True)
class MarkExecutionUnknown:
    submit_intent_id: str
    reason_code: ExecutionErrorCode
    observed_at_us: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]", self.submit_intent_id):
            raise InvalidExecutionRequestError("invalid unknown-execution identity")
        if not isinstance(self.reason_code, ExecutionErrorCode):
            raise InvalidExecutionRequestError("unknown execution needs a stable reason code")
        if type(self.observed_at_us) is not int or not 0 <= self.observed_at_us <= 2**53 - 1:
            raise InvalidExecutionRequestError("unknown observation needs a safe timestamp")

    @classmethod
    def for_intent(
        cls, intent: SubmissionIntent, *, reason_code: ExecutionErrorCode, at: datetime
    ) -> MarkExecutionUnknown:
        """Mark exactly the intent the caller still holds, at one bounded instant."""
        return cls(
            intent.identity.submit_intent_id,
            reason_code,
            observed_at_microseconds(at),
        )

    def apply(self, intent: SubmissionIntent, *, at: datetime) -> SubmissionIntent:
        if intent.identity.submit_intent_id != self.submit_intent_id:
            raise InvalidExecutionRequestError("unknown observation belongs to another execution")
        observed = _EPOCH + timedelta(microseconds=self.observed_at_us)
        if observed > at or observed < intent.recorded_at:
            raise InvalidExecutionRequestError("unknown observation contradicts execution time")
        if intent.execution_state.is_terminal:
            raise ExecutionCleanupPendingError("a terminal execution cannot become unknown")
        if intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED:
            if intent.error_code != self.reason_code:
                raise ExecutionObservationConflictError("unknown execution has another reason")
            return intent
        if observed < intent.updated_at or at < intent.updated_at:
            raise InvalidExecutionRequestError("a stale observation cannot replace a newer fact")
        if intent.version >= 2**63 - 1:
            raise SubmissionStoreError("execution version is exhausted")
        updated = deepcopy(intent)
        updated.execution_state = ExecutionState.UNKNOWN_QUARANTINED
        updated.cleanup_state = CleanupState.QUARANTINED
        updated.error_code = self.reason_code
        # No engine fact was obtained. Keep the original request ID and dispatch time,
        # including None after a crash between committing intent and recording dispatch.
        updated._touch(at)
        return updated


def execution_identity_fields(intent: SubmissionIntent) -> dict[str, object]:
    """Identity, deadline and dispatch facts every execution projection shares."""
    identity = intent.identity
    return {
        "submit_intent_id": identity.submit_intent_id,
        "lease_id": identity.lease_id,
        "operation_id": identity.operation_id,
        "attempt_id": identity.attempt_id,
        "stage_id": identity.stage_id,
        "workspace_id": identity.workspace_id,
        "pool_id": identity.pool_id,
        "host_node_id": identity.host_node_id,
        "gateway_id": identity.gateway_id,
        "instance_id": identity.instance_id,
        "instance_generation": identity.instance_generation,
        "binding_revision_id": identity.binding_revision_id,
        "policy_revision_id": identity.policy_revision_id,
        "voice_revision_id": identity.voice_revision_id,
        "fencing_token": identity.fencing_token,
        "command_digest": identity.command_digest,
        "deadline_at": contract_instant_text(identity.deadline_at),
        "dispatched_at": (
            None if intent.dispatched_at is None else contract_instant_text(intent.dispatched_at)
        ),
        "sample_rate_hz": identity.sample_rate_hz,
    }


def execution_state_fields(intent: SubmissionIntent) -> dict[str, object]:
    """State and evidence-kind facts, with the same substitution every route must serve."""
    return {
        "engine_request_id": intent.engine_request_id,
        "execution_state": intent.execution_state.value,
        "cleanup_state": intent.cleanup_state.value,
        "stop_evidence_kind": (
            "unconfirmed_timeout"
            if intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
            else intent.stop_evidence_kind.value
        ),
    }


def submission_response(intent: SubmissionIntent, *, idempotent_replay: bool) -> dict[str, object]:
    """The ``SubmitExecutionResponse`` shape for a recorded or replayed intent."""
    payload = execution_identity_fields(intent)
    payload.update(execution_state_fields(intent))
    payload.update(
        idempotent_replay=idempotent_replay,
        recorded_at=contract_instant_text(intent.recorded_at),
    )
    return payload


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    intent: SubmissionIntent

    def status(self) -> dict[str, object]:
        intent = self.intent
        identity = intent.identity
        payload = execution_identity_fields(intent)
        payload.update(execution_state_fields(intent))
        payload.update(
            error_code=None if intent.error_code is None else intent.error_code.value,
            updated_at=contract_instant_text(intent.updated_at),
            output_watermark=None,
        )
        if intent.output_stream_id is not None:
            payload["output_watermark"] = {
                "output_stream_id": intent.output_stream_id,
                "generated_pcm_samples": intent.generated_pcm_samples,
                "sample_rate_hz": identity.sample_rate_hz,
                "hard_output_ceiling_samples": hard_output_ceiling_samples(
                    max_output_audio_ms=identity.max_output_audio_ms,
                    sample_rate_hz=identity.sample_rate_hz,
                ),
            }
        # A private staging key alone is not a complete immutable artifact reference.
        return payload

    def response(self) -> dict[str, object]:
        return {"kind": "execution", "execution": self.status()}
