"""One bounded pass that returns capacity for leases no dispatch can reach (#1248).

The decision *which* leases are eligible is one SQL statement
(``infrastructure.lease_reconciliation_sql``); the decision *what a release means*
is the domain's (``domain.lease_release.release``), reached through the same
``LeaseService.release`` every other settlement path uses. This module only
sequences them and reports what happened, so it holds no counter arithmetic and no
second definition of "releasable".

A pass never raises for one lease: the sweep exists to converge a leak, and
stopping the whole pass because a single row was refused would leave the rest
leaking. Every refusal is reported with its stable code instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import LeaseResult

#: The largest pass a caller may ask for. The bound exists so one run is a short,
#: predictable transaction set: a backlog is drained across passes, not in one go.
MAX_SWEEP = 64
#: The reference cadence's default: a handful of rows per pass is enough to
#: converge a leak, and a CronJob repeats until the backlog is empty.
DEFAULT_SWEEP = 8

SweepDecision = Literal["released", "replayed", "planned", "refused", "unknown"]


@dataclass(frozen=True, slots=True)
class LeaseCandidate:
    """One row the selection statement returned, exactly as it was read."""

    lease_id: str
    version: int
    operation_id: str
    attempt_id: str
    stage_id: str
    reserved_slots: int
    attempt_quota_state: str

    def __post_init__(self) -> None:
        for identity in (self.lease_id, self.operation_id, self.attempt_id, self.stage_id):
            if not isinstance(identity, str) or not identity:
                raise ValueError("Lease candidate needs its recorded identity")
        for counter in (self.version, self.reserved_slots):
            if type(counter) is not int or counter < 1:
                raise ValueError("Lease candidate needs positive recorded counters")
        if not isinstance(self.attempt_quota_state, str) or not self.attempt_quota_state:
            raise ValueError("Lease candidate needs the locked attempt's quota state")


@dataclass(frozen=True, slots=True)
class LeaseSweep:
    """What one candidate became, and the stable code when it did not release."""

    candidate: LeaseCandidate
    decision: SweepDecision
    error_code: str | None = None

    def __post_init__(self) -> None:
        if (self.decision in ("refused", "unknown")) != (self.error_code is not None):
            raise ValueError("A refused or unknown sweep must name its code")
        if self.decision in ("released", "replayed") and self.error_code is not None:
            raise ValueError("A settled sweep cannot carry an error code")

    @property
    def returned_slots(self) -> int:
        """Slots this sweep returned; a replay returns nothing new.

        A released lease returns its own reservation, which is why the number is
        read from the candidate instead of recomputed from counters.
        """
        return self.candidate.reserved_slots if self.decision == "released" else 0


@dataclass(frozen=True, slots=True)
class LeaseReconciliationReport:
    """The bounded pass: one entry per candidate read, plus the limit it used."""

    limit: int
    sweeps: tuple[LeaseSweep, ...]

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_SWEEP:
            raise ValueError("A reconciliation report needs a bounded limit")
        if len(self.sweeps) > self.limit:
            raise ValueError("A pass cannot exceed the bound it was given")

    def _count(self, decision: SweepDecision) -> int:
        return sum(sweep.decision == decision for sweep in self.sweeps)

    @property
    def planned(self) -> int:
        return self._count("planned")

    @property
    def released(self) -> int:
        return self._count("released")

    @property
    def replayed(self) -> int:
        return self._count("replayed")

    @property
    def refused(self) -> int:
        return self._count("refused")

    @property
    def unknown(self) -> int:
        return self._count("unknown")

    @property
    def returned_slots(self) -> int:
        return sum(sweep.returned_slots for sweep in self.sweeps)


class LeaseReconciliationTransactions(Protocol):
    """The two ports one pass needs; the adapter owns transactions and locks."""

    def candidates(self, limit: int) -> tuple[LeaseCandidate, ...]: ...

    def release(self, candidate: LeaseCandidate) -> LeaseResult: ...


class LeaseReconciliation:
    """Run the bounded sweep against a lease-release identity."""

    def __init__(self, transactions: LeaseReconciliationTransactions) -> None:
        self._transactions = transactions

    def run_once(
        self, *, limit: int = DEFAULT_SWEEP, dry_run: bool = False
    ) -> LeaseReconciliationReport:
        """Read the bounded set and settle it; ``dry_run`` only names it."""
        if type(limit) is not int or not 1 <= limit <= MAX_SWEEP:
            raise ValueError("A reconciliation pass needs a bound within 1..MAX_SWEEP")
        if type(dry_run) is not bool:
            raise ValueError("A reconciliation pass needs a real dry-run flag")
        candidates = self._transactions.candidates(limit)
        if len(candidates) > limit:
            raise ValueError("The selection returned more rows than the bound")
        if dry_run:
            return LeaseReconciliationReport(
                limit, tuple(LeaseSweep(candidate, "planned") for candidate in candidates)
            )
        return LeaseReconciliationReport(
            limit, tuple(self._settle(candidate) for candidate in candidates)
        )

    def _settle(self, candidate: LeaseCandidate) -> LeaseSweep:
        try:
            result = self._transactions.release(candidate)
        except Exception:
            # A storage fault on one lease must not abandon the rest of the pass.
            # The row still reads ``reserved``, so the next pass retries it.
            return LeaseSweep(candidate, "unknown", "lease_storage_unavailable")
        if result.outcome in ("committed", "replayed"):
            if result.fact is None or result.fact.lease_state != "released":
                # A success that does not carry the released lease is a defect, not
                # a settled row: report it instead of counting capacity as returned.
                return LeaseSweep(candidate, "unknown", "reconciliation_required")
            decision: SweepDecision = "released" if result.outcome == "committed" else "replayed"
            return LeaseSweep(candidate, decision)
        if result.outcome == "conflict":
            return LeaseSweep(candidate, "refused", result.error_code or "version_conflict")
        if result.outcome == "rejected":
            return LeaseSweep(candidate, "refused", result.error_code or "reconciliation_required")
        return LeaseSweep(candidate, "unknown", result.error_code or "outcome_unknown")


def release_request(candidate: LeaseCandidate) -> ReleaseLease:
    """The existing release request type; the sweep invents no second request shape."""
    return ReleaseLease(candidate.lease_id, candidate.version)


__all__ = [
    "DEFAULT_SWEEP",
    "MAX_SWEEP",
    "LeaseCandidate",
    "LeaseReconciliation",
    "LeaseReconciliationReport",
    "LeaseReconciliationTransactions",
    "LeaseSweep",
    "release_request",
]
