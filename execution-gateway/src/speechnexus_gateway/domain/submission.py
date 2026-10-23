"""Submission intent identity, execution state and stop-evidence rules.

These rules mirror the invariants of ``contracts/internal/execution-control.openapi.yaml``:
an intent is persisted before an engine request, a repeated intent never causes a
second dispatch, an ambiguous dispatch stays quarantined while holding capacity,
and a failure that never reached the engine releases capacity as
``not_required`` instead of fabricating stop evidence.

The port that persists the intent is ``create_submit`` in
``contracts/internal/coordination-transactions.schema.json``: it validates the complete
execution request against the authoritative lease rows, writes one intent, and never
dispatches. A replay compares the whole effective request, so the request digest and its
canonical document travel with the intent.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from speechnexus_gateway.domain.canonical import JsonValue, canonical_json, command_digest

_DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAMPLE_RATES: Final = frozenset({16000, 24000, 48000})
_MIN_OUTPUT_AUDIO_MS: Final = 100
_MAX_INT64: Final = 2**63 - 1


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


class LeaseState(StrEnum):
    """Reservation states a submit may observe on the authoritative lease row."""

    RESERVED = "reserved"
    EXECUTING = "executing"
    STOPPING = "stopping"
    RELEASED = "released"
    QUARANTINED = "quarantined"

    @property
    def accepts_submit(self) -> bool:
        """Only a lease that has not stopped or been isolated may take a first submit."""
        return self in {LeaseState.RESERVED, LeaseState.EXECUTING}


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


class ComponentCredentialInvalidError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.COMPONENT_CREDENTIAL_INVALID


class SubmitIntentConflictError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.SUBMIT_INTENT_CONFLICT


class CancellationUnconfirmedError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.CANCELLATION_UNCONFIRMED


class ExecutionCleanupPendingError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.EXECUTION_CLEANUP_PENDING


class LeaseNotActiveError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.LEASE_NOT_ACTIVE


class StaleFencingTokenError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.STALE_FENCING_TOKEN


class InstanceGenerationMismatchError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.INSTANCE_GENERATION_MISMATCH


class GatewayBarrierFencedError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.GATEWAY_BARRIER_FENCED


class SubmissionOutcomeUnknownError(Exception):
    """The store cannot prove whether the transaction committed.

    Recovery is a read of the original identity; the caller must not repeat the write
    or dispatch as if the intent did not exist.
    """

    code: str = "outcome_unknown"
    recovery: str = "query_original"


class SubmissionStoreError(Exception):
    """A store failure that is not a business rejection of the request.

    ``code`` and ``recovery`` are the transaction error code and recovery hint from
    ``contracts/internal/coordination-transactions.schema.json``; an interface layer maps
    them instead of inventing new codes.
    """

    code: str = "reconciliation_required"
    recovery: str = "reconcile"


class SubmissionStoreUnavailableError(SubmissionStoreError):
    """No transaction was committed, so retrying the same request is safe."""

    recovery = "retry_transaction"


class SubmissionIntentExistsError(SubmissionStoreError):
    """The submit identity or its lease already owns a durable intent."""

    code = "idempotency_conflict"
    recovery = "query_original"


class StaleSubmissionVersionError(SubmissionStoreError):
    """The stored intent moved on since the caller read it."""

    code = "version_conflict"
    recovery = "query_current_authority"


class StopEvidenceConflictError(SubmissionStoreError):
    """An evidence identity or an already closed execution has another fact."""

    code = "idempotency_conflict"
    recovery = "query_original"


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
        if self.max_output_audio_ms is not None and self.max_output_audio_ms < _MIN_OUTPUT_AUDIO_MS:
            raise ValueError("max_output_audio_ms must be at least 100")


@dataclass(frozen=True, slots=True)
class LeaseFacts:
    """Authoritative lease and attempt facts, read under the submit port's row locks.

    A claim is never trusted on its own: the adapter locks the pool, the attempt and the
    lease, then :func:`verify_submit_identity` compares every shared field with these rows.
    """

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
    fencing_token: int
    lease_state: LeaseState
    binding_revision_id: str
    policy_revision_id: str


def verify_submit_identity(
    identity: SubmissionIdentity,
    facts: LeaseFacts,
    *,
    sealed_min_fencing_token: int | None,
) -> None:
    """Reject a submit that disagrees with the locked authority, or with a sealed barrier."""
    claimed = (
        ("lease_id", identity.lease_id, facts.lease_id),
        ("operation_id", identity.operation_id, facts.operation_id),
        ("attempt_id", identity.attempt_id, facts.attempt_id),
        ("stage_id", identity.stage_id, facts.stage_id),
        ("workspace_id", identity.workspace_id, facts.workspace_id),
        ("pool_id", identity.pool_id, facts.pool_id),
        ("host_node_id", identity.host_node_id, facts.host_node_id),
        ("gateway_id", identity.gateway_id, facts.gateway_id),
        ("instance_id", identity.instance_id, facts.instance_id),
        ("binding_revision_id", identity.binding_revision_id, facts.binding_revision_id),
        ("policy_revision_id", identity.policy_revision_id, facts.policy_revision_id),
    )
    for name, claimed_value, authoritative_value in claimed:
        if claimed_value != authoritative_value:
            raise InvalidExecutionRequestError(
                f"submit identity disagrees with the lease on {name}"
            )
    if identity.instance_generation != facts.instance_generation:
        raise InstanceGenerationMismatchError(
            "submit identity disagrees with the lease on instance_generation"
        )
    if identity.fencing_token != facts.fencing_token:
        raise StaleFencingTokenError("submit identity disagrees with the lease on fencing_token")
    if not facts.lease_state.accepts_submit:
        raise LeaseNotActiveError(f"lease is {facts.lease_state}")
    if sealed_min_fencing_token is not None and identity.fencing_token < sealed_min_fencing_token:
        raise GatewayBarrierFencedError(
            "a sealed gateway barrier already requires a higher fencing token"
        )


def hard_output_ceiling_samples(
    *, max_output_audio_ms: int | None, sample_rate_hz: int | None
) -> int | None:
    """Return the frozen sample ceiling, or ``None`` for a stage that emits no audio."""
    if (max_output_audio_ms is None) != (sample_rate_hz is None):
        raise ValueError("sample_rate_hz is required exactly when max_output_audio_ms is set")
    if max_output_audio_ms is None or sample_rate_hz is None:
        return None
    if max_output_audio_ms < _MIN_OUTPUT_AUDIO_MS:
        raise ValueError("max_output_audio_ms must be at least 100")
    if sample_rate_hz not in _SAMPLE_RATES:
        raise ValueError("sample_rate_hz must be 16000, 24000 or 48000")
    if max_output_audio_ms * sample_rate_hz > _MAX_INT64:
        raise ValueError("the output ceiling overflows a signed 64-bit sample count")
    return -(-max_output_audio_ms * sample_rate_hz // 1000)


def max_output_audio_ms_for(*, ceiling_samples: int, sample_rate_hz: int) -> int:
    """Invert :func:`hard_output_ceiling_samples` exactly, never by rounding down.

    A stored ceiling that does not represent a whole millisecond would silently widen or
    narrow the frozen output budget, so it is reported instead of reinterpreted.
    """
    if ceiling_samples < 0:
        raise ValueError("hard_output_ceiling_samples must not be negative")
    if sample_rate_hz not in _SAMPLE_RATES:
        raise ValueError("sample_rate_hz must be 16000, 24000 or 48000")
    scaled = ceiling_samples * 1000
    if scaled % sample_rate_hz:
        raise ValueError("hard_output_ceiling_samples is not a whole-millisecond ceiling")
    return scaled // sample_rate_hz


def contract_instant_text(value: datetime) -> str:
    """Serialize an instant the way the canonical request document must contain it.

    The gateway resolves the wire value before digesting anything, so the same instant is
    written the same way whatever offset the caller used.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution instants must be timezone aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SubmitRequest:
    """A submission as received at the gateway boundary.

    ``output_stream_id`` is the ``create_submit`` sibling of the execution request: the
    contract requires it exactly for stages that emit PCM, and forbids it otherwise.
    """

    identity: SubmissionIdentity
    command: JsonValue
    output_stream_id: str | None = None

    def __post_init__(self) -> None:
        if (self.output_stream_id is None) != (self.identity.sample_rate_hz is None):
            raise ValueError("output_stream_id is required exactly when sample_rate_hz is set")

    @property
    def effective_request(self) -> JsonValue:
        """The resolved request, in the shape the persisted document must carry."""
        identity = self.identity
        execution_request: dict[str, JsonValue] = {
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
            "command": self.command,
            "deadline_at": contract_instant_text(identity.deadline_at),
            "max_output_audio_ms": identity.max_output_audio_ms,
            "sample_rate_hz": identity.sample_rate_hz,
        }
        return {"execution_request": execution_request, "output_stream_id": self.output_stream_id}

    @property
    def request_document(self) -> str:
        """Canonical text persisted next to the intent so the request stays recoverable."""
        return canonical_json(self.effective_request)

    @property
    def request_digest(self) -> str:
        """Digest of the complete effective request, not only of ``command``."""
        return command_digest(self.effective_request)


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
    reason: str | None

    def __post_init__(self) -> None:
        if not _DIGEST_PATTERN.match(self.evidence_digest):
            raise ValueError("evidence_digest must be sha256:<64 lowercase hex>")
        identifier = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]")
        if not identifier.fullmatch(self.evidence_id) or not identifier.fullmatch(
            self.submit_intent_id
        ):
            raise ValueError("stop evidence identifiers must match the shared Identifier domain")
        if type(self.observed_at_us) is not int or not 0 <= self.observed_at_us <= 2**53 - 1:
            raise ValueError("observed_at_us must be a nonnegative safe integer")
        if self.reason is not None and (
            not self.reason.strip() or not 1 <= len(self.reason) <= 2000
        ):
            raise ValueError("reason must contain non-whitespace text of at most 2000 characters")
        if (
            self.evidence_kind is StopEvidenceKind.OPERATOR_VERIFIED_INSTANCE_TERMINATED
            and self.reason is None
        ):
            raise ValueError("operator verified stop requires a reason")

    @property
    def is_verified(self) -> bool:
        """True only for an observed stop with released resources."""
        if self.evidence_kind in {
            StopEvidenceKind.NONE,
            StopEvidenceKind.UNCONFIRMED_TIMEOUT,
        }:
            return False
        return self.execution_stopped is True and self.resources_released is True


@dataclass(slots=True)
class SubmissionIntent:
    """Durable submission record; capacity stays held while the state says so."""

    identity: SubmissionIdentity
    recorded_at: datetime
    updated_at: datetime
    request_digest: str
    output_stream_id: str | None = None
    execution_state: ExecutionState = ExecutionState.SUBMITTING
    cleanup_state: CleanupState = CleanupState.PENDING
    dispatched_at: datetime | None = None
    engine_request_id: str | None = None
    error_code: ExecutionErrorCode | None = None
    stop_evidence_kind: StopEvidenceKind = StopEvidenceKind.NONE
    stop_evidence_digest: str | None = None
    terminated_at: datetime | None = None
    generated_pcm_samples: int = 0
    version: int = field(default=1)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Enforce the contract's state, cleanup and dispatch-evidence matrix."""
        if not _DIGEST_PATTERN.match(self.request_digest):
            raise ValueError("request_digest must be sha256:<64 lowercase hex>")
        if (self.output_stream_id is None) != (self.identity.sample_rate_hz is None):
            raise ValueError("output_stream_id is required exactly when sample_rate_hz is set")
        ceiling = hard_output_ceiling_samples(
            max_output_audio_ms=self.identity.max_output_audio_ms,
            sample_rate_hz=self.identity.sample_rate_hz,
        )
        if (
            type(self.generated_pcm_samples) is not int
            or self.generated_pcm_samples < 0
            or (ceiling is None and self.generated_pcm_samples != 0)
            or (ceiling is not None and self.generated_pcm_samples > ceiling)
        ):
            raise ValueError("generated samples contradict the frozen output budget")
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
        }
        if self.execution_state in dispatched_states and self.dispatched_at is None:
            raise ValueError(f"{self.execution_state} requires a dispatch time")
        if (
            self.execution_state is ExecutionState.ABORTED
            and self.dispatched_at is None
            and (
                self.stop_evidence_kind
                not in {
                    StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
                    StopEvidenceKind.OPERATOR_VERIFIED_INSTANCE_TERMINATED,
                }
                or self.stop_evidence_digest is None
                or self.terminated_at is None
            )
        ):
            raise ValueError("unobserved dispatch requires durable instance termination evidence")
        if self.execution_state is ExecutionState.FAILED:
            if self.error_code is None:
                raise ValueError("failed intent carries a stable error code")
            if self.cleanup_state is CleanupState.NOT_REQUIRED and (
                self.dispatched_at is not None
                or self.engine_request_id is not None
                or self.generated_pcm_samples != 0
            ):
                raise ValueError(
                    "cleanup_state not_required requires no dispatch time, "
                    "no engine request and no generated output"
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
        self,
        evidence: VerifiedStopEvidence,
        *,
        terminal_state: ExecutionState,
        at: datetime,
        error_code: ExecutionErrorCode | None = None,
    ) -> None:
        """Apply a verified fact; the transaction port owns persistence and replay."""
        if evidence.submit_intent_id != self.identity.submit_intent_id:
            raise ValueError("stop evidence belongs to another submit intent")
        RecordStop(evidence, terminal_state, error_code)
        if self.execution_state.is_terminal:
            raise ExecutionCleanupPendingError("a terminal execution cannot be reversed")
        if self.dispatched_at is None and not (
            self.execution_state is ExecutionState.UNKNOWN_QUARANTINED
            and terminal_state is ExecutionState.ABORTED
            and evidence.evidence_kind
            in {
                StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
                StopEvidenceKind.OPERATOR_VERIFIED_INSTANCE_TERMINATED,
            }
        ):
            raise ExecutionCleanupPendingError("stop evidence requires a dispatched intent")
        if (
            self.execution_state is ExecutionState.UNKNOWN_QUARANTINED
            and terminal_state is not ExecutionState.ABORTED
        ):
            raise ExecutionCleanupPendingError("unknown execution needs abort or reconciliation")
        observed_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            microseconds=evidence.observed_at_us
        )
        earliest = self.dispatched_at or self.recorded_at
        if observed_at > at or observed_at < earliest or at < self.updated_at:
            raise ExecutionCleanupPendingError("stop observation contradicts execution time")
        if self.version >= _MAX_INT64:
            raise ExecutionCleanupPendingError("execution version is exhausted")
        self.execution_state = terminal_state
        self.cleanup_state = CleanupState.COMPLETE
        self.stop_evidence_kind = evidence.evidence_kind
        self.stop_evidence_digest = evidence.evidence_digest
        self.terminated_at = observed_at
        self.error_code = error_code
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


@dataclass(frozen=True, slots=True)
class RecordStop:
    """Frozen record_stop request with one shared transition and replay rule."""

    evidence: VerifiedStopEvidence
    terminal_state: ExecutionState
    error_code: ExecutionErrorCode | None = None

    def __post_init__(self) -> None:
        if not self.evidence.is_verified:
            raise CancellationUnconfirmedError("stop evidence is not confirmed")
        if not self.terminal_state.is_terminal:
            raise InvalidExecutionRequestError("stop evidence requires a terminal state")
        if (self.terminal_state is ExecutionState.FAILED) != (self.error_code is not None):
            raise InvalidExecutionRequestError("only a failed stop requires an error code")
        if (
            self.terminal_state is ExecutionState.SUCCEEDED
            and self.evidence.evidence_kind is not StopEvidenceKind.ENGINE_TERMINAL_RESPONSE
        ):
            raise InvalidExecutionRequestError("success requires the engine terminal response")

    def apply(
        self,
        intent: SubmissionIntent,
        *,
        previous_evidence: VerifiedStopEvidence | None,
        at: datetime,
    ) -> SubmissionIntent:
        """Return the original replay or one new version, never partially mutate a caller."""
        if previous_evidence is not None:
            if (
                previous_evidence != self.evidence
                or intent.execution_state is not self.terminal_state
                or intent.error_code != self.error_code
                or intent.cleanup_state is not CleanupState.COMPLETE
                or intent.stop_evidence_digest != self.evidence.evidence_digest
            ):
                raise StopEvidenceConflictError("stop identity already has another complete fact")
            return intent
        updated = deepcopy(intent)
        updated.apply_stop_evidence(
            self.evidence,
            terminal_state=self.terminal_state,
            error_code=self.error_code,
            at=at,
        )
        return updated
