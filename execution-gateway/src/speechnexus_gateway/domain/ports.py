"""Ports the gateway domain needs from the outside world.

Implementations live in ``infrastructure`` (adapters) or in tests (fakes); the
domain and application layers only see these protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from speechnexus_gateway.domain.submission import (
    DispatchResult,
    EngineObservedState,
    ExecutionErrorCode,
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


class SubmissionLedger(Protocol):
    """Durable intent store; the adapter owns the transaction and row version."""

    def get(self, submit_intent_id: str) -> SubmissionIntent | None: ...

    def add(self, intent: SubmissionIntent) -> None: ...

    def save(self, intent: SubmissionIntent, *, expected_version: int) -> None: ...


class EngineDispatchPort(Protocol):
    """Issues the frozen command to the engine exactly once per call."""

    def dispatch(self, intent: SubmissionIntent) -> DispatchOutcome: ...


class EngineObservationPort(Protocol):
    """Queries the engine by the identity the gateway already issued."""

    def observe(self, intent: SubmissionIntent) -> EngineObservation | None: ...
