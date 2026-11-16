"""Operator release of a completed execution's reservation: replay first, then one write.

This is the sibling of ``quarantine_releases`` and shares its two-level shape on
purpose: a committed ledger row answers every replay without taking the global
lock order, and only a first release enters the write transaction. The two differ
in the fact they release -- an unknown execution versus a completed one -- so
they are two services over one ledger, never one rule that accepts both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from speechnexus_backend.speech.domain.completed_release import (
    CompletedStopEvidence,
    DrainRecord,
)
from speechnexus_backend.speech.domain.quarantine_release import (
    ReleaseEvidence,
    ReleaseOutcome,
)


@dataclass(frozen=True, slots=True)
class CompletedLeaseSnapshot:
    """One lease as the operator flow reads it before and after a release.

    Every field is grantable to the dedicated release identity, so the operator
    tool can identify the target, print the capacity it is about to return and
    verify the result without the API runtime role or a second DSN.
    """

    lease_id: str
    operation_id: str
    attempt_id: str
    stage_id: str
    instance_id: str
    instance_generation: int
    lease_state: str
    lease_cleanup_state: str
    budget_released_at_us: int | None
    quarantine_observed_at_us: int | None
    submit_intent_id: str | None
    execution_state: str | None
    execution_cleanup_state: str | None
    recorded_at_us: int | None
    dispatched_at_us: int | None
    terminated_at_us: int | None
    intent_version: int | None
    stop: CompletedStopEvidence | None
    fence_accepted_at_us: int | None
    reconciliation_settled_at_us: int | None
    job_state: str | None
    instance_held_slots: int
    instance_quarantined_slots: int


class CompletedReleaseTransactions(Protocol):
    """Level-one ledger read plus the level-two completed-execution release."""

    def lookup(self, evidence: ReleaseEvidence) -> ReleaseOutcome | None: ...

    def commit_completed(self, evidence: ReleaseEvidence, drain: DrainRecord) -> ReleaseOutcome: ...

    def completed_snapshot(self, lease_id: str) -> CompletedLeaseSnapshot | None: ...


class CompletedLeaseReader(Protocol):
    """The read-only snapshot an operator tool needs to identify its target."""

    def completed_snapshot(self, lease_id: str) -> CompletedLeaseSnapshot | None: ...


class CompletedReleaseReleaser(Protocol):
    """The one write the operator tool performs: release, or replay the ledger."""

    def release(self, evidence: ReleaseEvidence, drain: DrainRecord) -> ReleaseOutcome: ...


class CompletedReleaseService:
    """Release a completed execution's reservation from one pinned testimony.

    ``drain`` is the gateway's own drain response for the lease's instance
    generation, recorded by the operator. It is required on the first release and
    ignored on a replay: a committed ledger row is the final fact, and demanding
    a fresh drain for it would make an idempotent retry depend on a second
    gateway read.
    """

    def __init__(self, transactions: CompletedReleaseTransactions) -> None:
        self._transactions = transactions

    def release(self, evidence: ReleaseEvidence, drain: DrainRecord) -> ReleaseOutcome:
        replay = self._transactions.lookup(evidence)
        if replay is not None:
            return replay
        return self._transactions.commit_completed(evidence, drain)


__all__ = [
    "CompletedLeaseReader",
    "CompletedLeaseSnapshot",
    "CompletedReleaseReleaser",
    "CompletedReleaseService",
    "CompletedReleaseTransactions",
]
