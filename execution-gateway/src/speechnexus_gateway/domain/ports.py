"""Ports the gateway domain needs from the outside world.

Implementations live in ``infrastructure`` (adapters) or in tests (fakes); the
domain and application layers only see these protocols.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from speechnexus_gateway.domain.fencing import BarrierSnapshot, IngressFence
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.recovery import ExecutionSnapshot, MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    EngineObservedState,
    ExecutionErrorCode,
    RecordStop,
    SubmissionIntent,
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


class ExecutionQueryPort(Protocol):
    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None: ...
