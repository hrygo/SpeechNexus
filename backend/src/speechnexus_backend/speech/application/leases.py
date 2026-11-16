"""Lease isolation, settlement and original queries; no engine stop or dispatch authority."""

from dataclasses import dataclass
from typing import Protocol

from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import LeaseResult, QuarantineLease


@dataclass(frozen=True, slots=True)
class LeaseSnapshot:
    """One admission lease and the gateway facts recorded against it.

    ``execution_state`` is the gateway's own durable fact for the lease's submit
    intent and is ``None`` when the lease never reached submission. It is what
    decides whether a quarantine is authorized, so it is carried here instead of
    being re-read later.
    """

    lease_id: str
    lease_state: str
    cleanup_state: str
    reserved_slots: int
    execution_state: str | None
    execution_cleanup_state: str | None


@dataclass(frozen=True, slots=True)
class LeaseIsolationGap:
    """One lease that did not move into the quarantined bucket, and why.

    ``reason`` is the lease-side refusal (``execution_cleanup_pending`` when the
    execution never became ``unknown_quarantined``/``quarantined``,
    ``lease_storage_unavailable`` when the write could not be confirmed) so an
    operator can tell "still running" from "unknown" without re-deriving it.
    """

    lease_id: str
    lease_state: str
    cleanup_state: str
    execution_state: str | None
    execution_cleanup_state: str | None
    reserved_slots: int
    reason: str


@dataclass(frozen=True, slots=True)
class LeaseIsolation:
    """Per-lease result of isolating an operation whose stop cannot be proven.

    ``complete`` is the only condition under which a Job may be closed as an
    unconfirmed cancellation: capacity that is still reported as held must never
    be described as quarantined, and a partially isolated operation would do
    exactly that (#852 review P1).
    """

    operation_id: str
    quarantined: tuple[str, ...]
    gaps: tuple[LeaseIsolationGap, ...]

    @property
    def complete(self) -> bool:
        return not self.gaps

    @property
    def held_slots(self) -> int:
        return sum(gap.reserved_slots for gap in self.gaps)

    def detail(self) -> str:
        """One bounded, secret-free sentence naming what still blocks closure."""
        if not self.gaps:
            return f"isolated {len(self.quarantined)} lease(s)"
        parts = ", ".join(
            f"{gap.lease_id} lease={gap.lease_state} "
            f"execution={gap.execution_state or 'not_submitted'} ({gap.reason})"
            for gap in self.gaps
        )
        return f"{len(self.gaps)} lease(s) not isolated: {parts}"


class LeaseTransactions(Protocol):
    def quarantine_lease(self, request: QuarantineLease) -> LeaseResult: ...

    def query_lease(self, lease_id: str) -> LeaseResult: ...

    def release_lease(self, request: ReleaseLease) -> LeaseResult: ...


class LeaseService:
    def __init__(self, transactions: LeaseTransactions) -> None:
        self._transactions = transactions

    def quarantine(self, request: QuarantineLease) -> LeaseResult:
        return self._transactions.quarantine_lease(request)

    def query(self, lease_id: str) -> LeaseResult:
        return self._transactions.query_lease(lease_id)

    def release(self, request: ReleaseLease) -> LeaseResult:
        return self._transactions.release_lease(request)
