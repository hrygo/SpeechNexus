"""Submission intent identity, execution state and stop-evidence rules.

These rules mirror the invariants of ``contracts/internal/execution-control.openapi.yaml``:
an intent is persisted before an engine request, a repeated intent never causes a
second dispatch, an ambiguous dispatch stays quarantined while holding capacity,
and a failure that never reached the engine releases capacity as
``not_required`` instead of fabricating stop evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

from speechnexus_gateway.domain.canonical import JsonValue

_DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAMPLE_RATES: Final = frozenset({16000, 24000, 48000})


class ExecutionState(StrEnum):
    """Authoritative gateway execution state."""

    SUBMITTING = "submitting"
    EXECUTING = "executing"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"
    UNKNOWN_QUARANTINED = "unknown_quarantined"

    @property
    def holds_capacity(self) -> bool:
        """Whether the state still owns reserved capacity."""
        return self in {
            ExecutionState.SUBMITTING,
            ExecutionState.EXECUTING,
            ExecutionState.STOPPING,
            ExecutionState.UNKNOWN_QUARANTINED,
        }

    @property
    def is_terminal(self) -> bool:
        return self in {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.ABORTED}


class CleanupState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    COMPLETE = "complete"
    QUARANTINED = "quarantined"


class StopEvidenceKind(StrEnum):
    NONE = "none"
    ENGINE_ABORT_ACK = "engine_abort_ack"
    ENGINE_TERMINAL_RESPONSE = "engine_terminal_response"
    INSTANCE_TERMINATED_BY_CONTROLLER = "instance_terminated_by_controller"
    OPERATOR_VERIFIED_INSTANCE_TERMINATED = "operator_verified_instance_terminated"
    UNCONFIRMED_TIMEOUT = "unconfirmed_timeout"


class ExecutionErrorCode(StrEnum):
    STALE_FENCING_TOKEN = "stale_fencing_token"
    GATEWAY_BARRIER_FENCED = "gateway_barrier_fenced"
    SUBMIT_INTENT_CONFLICT = "submit_intent_conflict"
    INSTANCE_GENERATION_MISMATCH = "instance_generation_mismatch"
    INSTANCE_DRAINING = "instance_draining"
    LEASE_NOT_ACTIVE = "lease_not_active"
    POLICY_OR_RESOURCE_DISABLED = "policy_or_resource_disabled"
    ENGINE_TRANSPORT_UNKNOWN = "engine_transport_unknown"
    CANCELLATION_UNCONFIRMED = "cancellation_unconfirmed"
    EXECUTION_CLEANUP_PENDING = "execution_cleanup_pending"
    INVALID_EXECUTION_REQUEST = "invalid_execution_request"
    COMPONENT_CREDENTIAL_INVALID = "component_credential_invalid"


class DispatchResult(StrEnum):
    """What the gateway knows after attempting one engine request."""

    ACKNOWLEDGED = "acknowledged"
    NOT_SENT = "not_sent"
    RESPONSE_LOST = "response_lost"


class EngineObservedState(StrEnum):
    """Outcome the engine reports for a request identity the gateway already issued."""

    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_CLEANUP_BY_STATE: Final[dict[ExecutionState, frozenset[CleanupState]]] = {
    ExecutionState.SUBMITTING: frozenset({CleanupState.PENDING}),
    ExecutionState.EXECUTING: frozenset({CleanupState.PENDING}),
    ExecutionState.STOPPING: frozenset({CleanupState.PENDING}),
    ExecutionState.SUCCEEDED: frozenset({CleanupState.COMPLETE}),
    ExecutionState.FAILED: frozenset({CleanupState.NOT_REQUIRED, CleanupState.COMPLETE}),
    ExecutionState.ABORTED: frozenset({CleanupState.COMPLETE}),
    ExecutionState.UNKNOWN_QUARANTINED: frozenset({CleanupState.QUARANTINED}),
}


class ExecutionRejectedError(Exception):
    """A submission or stop request the gateway refuses to accept."""

    error_code: ExecutionErrorCode


class InvalidExecutionRequestError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.INVALID_EXECUTION_REQUEST


class SubmitIntentConflictError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.SUBMIT_INTENT_CONFLICT


class CancellationUnconfirmedError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.CANCELLATION_UNCONFIRMED


@dataclass(frozen=True, slots=True)
class SubmissionIdentity:
    """Frozen identity and budget identity of one attempt."""

    submit_intent_id: str
    lease_id: str
    operation_id: str
    attempt_id: str
    stage_id: str
    workspace_id: str
    pool_id: str
    host_node_id: str
    gateway_id: str
    instance_id: str
    instance_generation: int
    binding_revision_id: str
    policy_revision_id: str
    voice_revision_id: str | None
    fencing_token: int
    command_digest: str
    deadline_at: datetime
    max_output_audio_ms: int | None = None
    sample_rate_hz: int | None = None

    def __post_init__(self) -> None:
        if not _DIGEST_PATTERN.match(self.command_digest):
            raise ValueError("command_digest must be sha256:<64 lowercase hex>")
        if self.instance_generation < 1:
            raise ValueError("instance_generation must be positive")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if (self.max_output_audio_ms is None) != (self.sample_rate_hz is None):
            raise ValueError("sample_rate_hz is required exactly when max_output_audio_ms is set")
        if self.sample_rate_hz is not None and self.sample_rate_hz not in _SAMPLE_RATES:
            raise ValueError("sample_rate_hz must be 16000, 24000 or 48000")


@dataclass(frozen=True, slots=True)
class SubmitRequest:
    """A submission as received at the gateway boundary."""

    identity: SubmissionIdentity
    command: JsonValue


@dataclass(frozen=True, slots=True)
class VerifiedStopEvidence:
    """Stop evidence that can release capacity only with both confirmations."""

    evidence_id: str
    submit_intent_id: str
    evidence_kind: StopEvidenceKind
    evidence_digest: str
    observed_at_us: int
    execution_stopped: bool
    resources_released: bool
    reason: str

    def __post_init__(self) -> None:
        if not _DIGEST_PATTERN.match(self.evidence_digest):
            raise ValueError("evidence_digest must be sha256:<64 lowercase hex>")

    @property
    def is_verified(self) -> bool:
        """True only for an observed stop with released resources."""
        if self.evidence_kind in {
            StopEvidenceKind.NONE,
            StopEvidenceKind.UNCONFIRMED_TIMEOUT,
        }:
            return False
        return self.execution_stopped and self.resources_released


@dataclass(slots=True)
class SubmissionIntent:
    """Durable submission record; capacity stays held while the state says so."""

    identity: SubmissionIdentity
    recorded_at: datetime
    updated_at: datetime
    execution_state: ExecutionState = ExecutionState.SUBMITTING
    cleanup_state: CleanupState = CleanupState.PENDING
    dispatched_at: datetime | None = None
    engine_request_id: str | None = None
    error_code: ExecutionErrorCode | None = None
    stop_evidence_kind: StopEvidenceKind = StopEvidenceKind.NONE
    stop_evidence_digest: str | None = None
    version: int = field(default=1)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Enforce the contract's state, cleanup and dispatch-evidence matrix."""
        allowed = _CLEANUP_BY_STATE[self.execution_state]
        if self.cleanup_state not in allowed:
            raise ValueError(
                f"{self.execution_state} cannot use cleanup_state {self.cleanup_state}"
            )
        if self.execution_state is ExecutionState.SUBMITTING and self.dispatched_at is not None:
            raise ValueError("submitting intent records no dispatch time")
        dispatched_states = {
            ExecutionState.EXECUTING,
            ExecutionState.STOPPING,
            ExecutionState.SUCCEEDED,
            ExecutionState.ABORTED,
        }
        if self.execution_state in dispatched_states and self.dispatched_at is None:
            raise ValueError(f"{self.execution_state} requires a dispatch time")
        if self.execution_state is ExecutionState.FAILED:
            if self.error_code is None:
                raise ValueError("failed intent carries a stable error code")
            if self.cleanup_state is CleanupState.NOT_REQUIRED and (
                self.dispatched_at is not None or self.engine_request_id is not None
            ):
                raise ValueError(
                    "cleanup_state not_required requires no dispatch time and no engine request"
                )
            if self.cleanup_state is CleanupState.COMPLETE and self.dispatched_at is None:
                raise ValueError("failed-with-cleanup intent requires a dispatch time")
        if self.execution_state is ExecutionState.SUCCEEDED:
            if self.error_code is not None:
                raise ValueError("successful intent carries no error code")
            if self.stop_evidence_kind is not StopEvidenceKind.ENGINE_TERMINAL_RESPONSE:
                raise ValueError("successful intent records the engine terminal response")

    def mark_dispatched(self, engine_request_id: str, *, at: datetime) -> None:
        self.execution_state = ExecutionState.EXECUTING
        self.cleanup_state = CleanupState.PENDING
        self.dispatched_at = at
        self.engine_request_id = engine_request_id
        self._touch(at)

    def mark_dispatch_unknown(self, *, at: datetime) -> None:
        """Keep capacity: the engine request may exist without a confirmation."""
        self.execution_state = ExecutionState.UNKNOWN_QUARANTINED
        self.cleanup_state = CleanupState.QUARANTINED
        self.dispatched_at = at
        self.error_code = ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN
        self._touch(at)

    def mark_not_dispatched_failure(self, error_code: ExecutionErrorCode, *, at: datetime) -> None:
        """Release capacity in the ``not_required`` branch, never as a stop."""
        self.execution_state = ExecutionState.FAILED
        self.cleanup_state = CleanupState.NOT_REQUIRED
        self.dispatched_at = None
        self.engine_request_id = None
        self.error_code = error_code
        self.stop_evidence_kind = StopEvidenceKind.NONE
        self.stop_evidence_digest = None
        self._touch(at)

    def mark_stopping(self, *, at: datetime) -> None:
        self.execution_state = ExecutionState.STOPPING
        self.cleanup_state = CleanupState.PENDING
        self._touch(at)

    def apply_stop_evidence(
        self, evidence: VerifiedStopEvidence, *, terminal_state: ExecutionState, at: datetime
    ) -> None:
        if evidence.submit_intent_id != self.identity.submit_intent_id:
            raise ValueError("stop evidence belongs to another submit intent")
        if terminal_state not in {ExecutionState.ABORTED, ExecutionState.FAILED}:
            raise ValueError("stop evidence only produces aborted or failed states")
        if not evidence.is_verified:
            raise CancellationUnconfirmedError("stop evidence is not confirmed")
        if self.dispatched_at is None:
            raise ValueError("stop evidence requires a dispatched intent")
        self.execution_state = terminal_state
        self.cleanup_state = CleanupState.COMPLETE
        self.stop_evidence_kind = evidence.evidence_kind
        self.stop_evidence_digest = evidence.evidence_digest
        self.error_code = None if terminal_state is ExecutionState.ABORTED else self.error_code
        self._touch(at)

    def complete_successfully(self, *, at: datetime) -> None:
        self.execution_state = ExecutionState.SUCCEEDED
        self.cleanup_state = CleanupState.COMPLETE
        self.stop_evidence_kind = StopEvidenceKind.ENGINE_TERMINAL_RESPONSE
        self.error_code = None
        self._touch(at)

    def adopt_engine_observation(
        self,
        *,
        engine_request_id: str,
        engine_state: EngineObservedState,
        at: datetime,
        error_code: ExecutionErrorCode | None = None,
    ) -> None:
        """Adopt an engine fact for a request the gateway already issued."""
        self.dispatched_at = self.dispatched_at or at
        self.engine_request_id = engine_request_id
        if engine_state is EngineObservedState.EXECUTING:
            self.execution_state = ExecutionState.EXECUTING
            self.cleanup_state = CleanupState.PENDING
            self.error_code = None
        elif engine_state is EngineObservedState.SUCCEEDED:
            self.execution_state = ExecutionState.SUCCEEDED
            self.cleanup_state = CleanupState.COMPLETE
            self.stop_evidence_kind = StopEvidenceKind.ENGINE_TERMINAL_RESPONSE
            self.error_code = None
        else:
            self.execution_state = ExecutionState.FAILED
            self.cleanup_state = CleanupState.COMPLETE
            self.error_code = error_code or ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN
        self._touch(at)

    def _touch(self, at: datetime) -> None:
        self.version += 1
        self.updated_at = at
        self.validate()
