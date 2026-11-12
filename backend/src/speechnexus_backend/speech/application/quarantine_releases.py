"""Operator quarantine release: replay first, then one bounded write."""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.speech.domain.quarantine_release import (
    ReleaseEvidence,
    ReleaseOutcome,
)


class QuarantineUnavailableError(Exception):
    """The transaction never started; a retry can succeed with the same intent."""


class QuarantineUnknownError(Exception):
    """The commit result is unknown; the caller must query before retrying."""


class QuarantineReleaseTransactions(Protocol):
    def lookup(self, evidence: ReleaseEvidence) -> ReleaseOutcome | None: ...

    def commit(self, evidence: ReleaseEvidence) -> ReleaseOutcome: ...


class QuarantineReleaseService:
    """The two-level read is a boundary, not an optimization.

    A committed ledger row answers every replay, so a repeat request never takes
    the global lock order. Only when no row exists does the write transaction
    run, and it re-reads the ledger after acquiring the locks because a
    concurrent commit may have landed in between.
    """

    def __init__(self, transactions: QuarantineReleaseTransactions) -> None:
        self._transactions = transactions

    def release(self, evidence: ReleaseEvidence) -> ReleaseOutcome:
        replay = self._transactions.lookup(evidence)
        if replay is not None:
            return replay
        return self._transactions.commit(evidence)


__all__ = [
    "QuarantineReleaseService",
    "QuarantineReleaseTransactions",
    "QuarantineUnavailableError",
    "QuarantineUnknownError",
]
