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
from speechnexus_gateway.domain.instance_drain import DrainInventory, InstanceIdentity
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.output_intervals import OutputIntervalRecord, OutputIntervalWrite
from speechnexus_gateway.domain.recovery import (
    ExecutionSnapshot,
    InstanceTerminationEvidence,
    MarkExecutionUnknown,
)
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    EngineObservedState,
    ExecutionErrorCode,
    RecordStop,
    SubmissionIntent,
    SubmitRequest,
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


class OriginalExecutionCommandSource(Protocol):
    """Read the verified frozen request; a read never authorizes redispatch."""

    def read_request(
        self, intent: SubmissionIntent, *, max_document_bytes: int
    ) -> SubmitRequest: ...


class CommandEngineDispatchPort(Protocol):
    """Accept one complete effective request, without rereading mutable defaults."""

    def dispatch(self, request: SubmitRequest) -> DispatchOutcome: ...


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


class InstanceTerminationEvidencePort(Protocol):
    """Reports whole-instance termination facts for one uncertain execution.

    An implementation answers with the producer's own observation or ``None``. A transport
    failure, timeout, disconnect or missing record is never a terminated instance: the
    caller keeps the execution quarantined and holding capacity in that case.
    """

    def termination_evidence(
        self, intent: SubmissionIntent
    ) -> InstanceTerminationEvidence | None: ...


class ExecutionRecoverySource(Protocol):
    """Read uncertain executions by immutable ID, strictly after a local cursor."""

    def list_quarantined(
        self,
        *,
        gateway_id: str,
        host_node_id: str,
        limit: int,
        after_submit_intent_id: str | None = None,
    ) -> list[SubmissionIntent]: ...


class ExecutionSweepLedger(SubmissionLedger, ExecutionRecoverySource, Protocol):
    """The intent store a recovery sweep enumerates and then closes through."""


class ExecutionRecoveryQuery(Protocol):
    """Observe the original execution, or read its recorded fact; never redispatch."""

    def query(self, submit_intent_id: str) -> SubmissionIntent | None: ...


class IngressOwnership(Protocol):
    @property
    def node_id(self) -> str: ...

    @property
    def gateway_id(self) -> str: ...

    def bind_ingress(self) -> None: ...

    def hold(self) -> AbstractContextManager[None]: ...


class BarrierLedger(Protocol):
    def seal_barrier(self, fence: IngressFence) -> BarrierSnapshot: ...

    def get_barrier(self, gateway_id: str, pool_id: str) -> BarrierSnapshot | None:
        """Read the recorded barrier fact without locking it or accepting a new minimum.

        A repeated command must return the original fact even after the owner that sealed it
        was replaced, so a read cannot go through the sealing authority check.
        """
        ...


class ExecutionRecoveryLedger(Protocol):
    def mark_execution_unknown(self, request: MarkExecutionUnknown) -> SubmissionIntent: ...


class ExecutionFactLedger(SubmissionLedger, ExecutionRecoveryLedger, Protocol):
    """One execution fact needs the recorded intent, its stop evidence and its unknown state."""


class ExecutionQueryPort(Protocol):
    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None: ...


class InstanceDrainLedger(BarrierLedger, ExecutionFactLedger, Protocol):
    """The recorded identity and the in-flight inventory of one instance generation.

    Both reads are ordinary reads: a drain reports what is still unfinished and never
    releases capacity, which the Scheduler owns.
    """

    def read_instance_identity(
        self, instance_id: str, instance_generation: int
    ) -> InstanceIdentity | None: ...

    def read_drain_inventory(self, identity: InstanceIdentity) -> DrainInventory: ...


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
