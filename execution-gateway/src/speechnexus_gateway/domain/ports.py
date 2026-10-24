"""Ports the gateway domain needs from the outside world.

Implementations live in ``infrastructure`` (adapters) or in tests (fakes); the
domain and application layers only see these protocols.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    CommandRedactionResult,
    RedactExecutionCommand,
)
from speechnexus_gateway.domain.fencing import BarrierSnapshot, IngressFence
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.output_intervals import OutputIntervalRecord, OutputIntervalWrite
from speechnexus_gateway.domain.recovery import ExecutionSnapshot, MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    EngineObservedState,
    ExecutionErrorCode,
    RecordStop,
    SubmissionIntent,
    VerifiedStopEvidence,
)


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """Result of exactly one attempt to hand an intent to the engine."""

    result: DispatchResult
    observed_at: datetime
    engine_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class EngineObservation:
    """What the engine reports for an already-issued request identity."""

    engine_request_id: str
    state: EngineObservedState
    error_code: ExecutionErrorCode | None = None
    dispatched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AbortOutcome:
    """What one bounded abort attempt learned about an already-issued identity.

    ``evidence`` carries the two release confirmations only when the adapter itself
    observed them. An unconfirmed attempt reports no evidence: the execution keeps
    holding capacity instead of being reported as stopped.
    """

    observed_at: datetime
    evidence: VerifiedStopEvidence | None = None
    error_code: ExecutionErrorCode | None = None
    engine_request_id: str | None = None

    @property
    def verified_stop(self) -> bool:
        return self.evidence is not None and self.evidence.is_verified


class ExecutionObservationLedger(Protocol):
    def record_execution_observation(
        self, request: RecordExecutionObservation
    ) -> SubmissionIntent: ...


class SubmissionLedger(ExecutionObservationLedger, Protocol):
    """Durable intent store; the adapter owns the transaction and row version.

    ``add`` is the ``create_submit`` transaction: it re-reads the authoritative lease rows
    under lock, stores one intent plus the canonical request document, and fails rather than
    returning a pre-existing intent, so a caller never dispatches for a record it did not
    just create. Observations use a guarded, versioned transaction.
    """

    def get(self, submit_intent_id: str) -> SubmissionIntent | None: ...

    def add(self, intent: SubmissionIntent, *, request_document: str) -> None: ...

    def record_stop(self, request: RecordStop) -> SubmissionIntent:
        """Append immutable evidence and close the execution in one transaction."""
        ...


class EngineDispatchPort(Protocol):
    """Issues the frozen command to the engine exactly once per call."""

    def dispatch(self, intent: SubmissionIntent) -> DispatchOutcome: ...


class EngineObservationPort(Protocol):
    """Queries the engine by the identity the gateway already issued."""

    def observe(self, intent: SubmissionIntent) -> EngineObservation | None: ...


class EngineAbortPort(Protocol):
    """Requests engine abort for one issued identity within the caller's bounded wait.

    An implementation must not synthesize stop evidence, not treat its own transport
    timeout, disconnect or a "not found" answer as release, and not exceed the wait it
    was given. Unconfirmed work stays quarantined and keeps holding capacity.
    """

    def abort(
        self, intent: SubmissionIntent, *, reason: str, wait_timeout_ms: int
    ) -> AbortOutcome: ...


class IngressOwnership(Protocol):
    @property
    def node_id(self) -> str: ...

    @property
    def gateway_id(self) -> str: ...

    def bind_ingress(self) -> None: ...

    def hold(self) -> AbstractContextManager[None]: ...


class BarrierLedger(Protocol):
    def seal_barrier(self, fence: IngressFence) -> BarrierSnapshot: ...


class ExecutionRecoveryLedger(Protocol):
    def mark_execution_unknown(self, request: MarkExecutionUnknown) -> SubmissionIntent: ...


class ExecutionAbortLedger(SubmissionLedger, ExecutionRecoveryLedger, Protocol):
    """One abort needs the recorded intent, verified stop evidence and the unknown transition."""


class ExecutionQueryPort(Protocol):
    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None: ...


class ExecutionCommandDeletionAuthorizer(Protocol):
    """Verify the durable deletion job and immutable dependency scope before issuing a permit.

    No default or wire-derived implementation is provided. An accepted durable deletion
    scope must already prevent new references/executions, including during this transaction.
    """

    def authorize(self, request: RedactExecutionCommand) -> AuthorizedCommandRedaction: ...


class ExecutionCommandRedactionLedger(Protocol):
    def redact_execution_command(
        self, permit: AuthorizedCommandRedaction
    ) -> CommandRedactionResult: ...


class OutputIntervalLedger(ExecutionQueryPort, Protocol):
    def append_interval(self, record: OutputIntervalRecord) -> OutputIntervalWrite: ...

    def query_output_interval(self, interval_id: str) -> OutputIntervalRecord | None: ...
