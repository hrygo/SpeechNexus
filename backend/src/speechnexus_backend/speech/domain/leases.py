"""Lease isolation preserves physical occupancy; it never proves execution stopped."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

from speechnexus_backend.speech.domain.capacity import (
    MemoryArchitecture,
    PhysicalResources,
    reserve_resources,
)
from speechnexus_backend.speech.domain.owners import IDENTIFIER, MAX_VERSION
from speechnexus_backend.speech.domain.runtime_intents import identifier, integer


@dataclass(frozen=True, slots=True)
class QuarantineLease:
    lease_id: str
    reason_code: str
    observed_at_us: int

    def __post_init__(self) -> None:
        identifier(self.lease_id)
        integer(self.observed_at_us)
        if not isinstance(self.reason_code, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", self.reason_code
        ):
            raise ValueError("Invalid lease reason code")


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    lease_id: str
    lease_state: str
    cleanup_state: str
    version: int
    reserved: PhysicalResources
    reserved_slots: int
    observed_at_us: int | None = None
    reason_code: str | None = None
    budget_released_at_us: int | None = None

    def __post_init__(self) -> None:
        identifier(self.lease_id)
        integer(self.version, minimum=1, maximum=MAX_VERSION)
        integer(self.reserved_slots, minimum=1)
        if self.lease_state not in ("reserved", "executing", "stopping", "quarantined", "released"):
            raise ValueError("Invalid lease state")
        if self.cleanup_state not in ("not_required", "pending", "complete", "quarantined"):
            raise ValueError("Invalid lease cleanup")
        if (self.observed_at_us is None) != (self.reason_code is None):
            raise ValueError("Incomplete first quarantine observation")
        if self.observed_at_us is not None:
            integer(self.observed_at_us)
            QuarantineLease(self.lease_id, self.reason_code or "", self.observed_at_us)
        if self.budget_released_at_us is not None:
            integer(self.budget_released_at_us)
        if self.lease_state == "quarantined" and (
            self.observed_at_us is None
            or self.cleanup_state != "quarantined"
            or self.budget_released_at_us is not None
        ):
            raise ValueError("Incomplete quarantined lease")


@dataclass(frozen=True, slots=True)
class LeaseAccounting:
    architecture: MemoryArchitecture
    capacity: PhysicalResources
    held: PhysicalResources
    quarantined: PhysicalResources
    node_state: str
    threshold_ppm: int
    held_slots: int
    quarantined_slots: int
    max_slots: int
    held_attempts: int
    quarantined_attempts: int
    max_attempts: int
    quota_state: str
    host_version: int
    instance_version: int
    attempt_version: int
    quota_version: int

    def __post_init__(self) -> None:
        for vector in (self.capacity, self.held, self.quarantined):
            vector.require_architecture(self.architecture)
        reserve_resources(
            capacity=self.capacity,
            held=self.held,
            quarantined=self.quarantined,
            requested=PhysicalResources.zero(),
        )
        integer(self.threshold_ppm, maximum=1000000)
        for value in (
            self.held_slots,
            self.quarantined_slots,
            self.max_slots,
            self.held_attempts,
            self.quarantined_attempts,
            self.max_attempts,
        ):
            integer(value)
        if (
            self.held_slots + self.quarantined_slots > self.max_slots
            or self.held_attempts + self.quarantined_attempts > self.max_attempts
            or self.quota_state not in ("held", "quarantined", "released")
            or not isinstance(self.node_state, str)
            or not self.node_state
        ):
            raise ValueError("Invalid lease accounting")
        for value in (
            self.host_version,
            self.instance_version,
            self.attempt_version,
            self.quota_version,
        ):
            integer(value, minimum=1, maximum=MAX_VERSION)


class LeaseRejectedError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class QuarantineDecision:
    lease: LeaseRecord
    accounting: LeaseAccounting
    replayed: bool


def _above_threshold(used: int, capacity: int, ppm: int) -> bool:
    # Exact integer comparison without multiplying a safe 53-bit capacity by
    # one million (which could exceed signed 64-bit intermediate bounds).
    whole, remainder = divmod(capacity, 1000000)
    limit = whole * ppm + remainder * ppm // 1000000
    return used > limit


def quarantine(
    lease: LeaseRecord,
    accounting: LeaseAccounting,
    request: QuarantineLease,
    *,
    execution: tuple[str, str] | None,
    now_us: int,
) -> QuarantineDecision:
    integer(now_us)
    if lease.lease_id != request.lease_id:
        raise LeaseRejectedError("reconciliation_required")
    if request.observed_at_us > now_us:
        raise LeaseRejectedError("invalid_execution_request")
    if lease.lease_state == "released":
        raise LeaseRejectedError("lease_not_active")
    if lease.lease_state == "quarantined":
        if lease.reason_code != request.reason_code:
            raise LeaseRejectedError("idempotency_conflict")
        # A later stop can change gateway facts. It cannot rewrite this first
        # observation, or turn its replay into a new budget movement.
        return QuarantineDecision(lease, accounting, True)
    if execution != ("unknown_quarantined", "quarantined"):
        raise LeaseRejectedError("execution_cleanup_pending")
    if (
        lease.budget_released_at_us is not None
        or lease.observed_at_us is not None
        or lease.cleanup_state != "pending"
        or accounting.quota_state == "released"
    ):
        raise LeaseRejectedError("reconciliation_required")
    try:
        lease.reserved.require_architecture(accounting.architecture)
        held = accounting.held.subtract(lease.reserved)
        isolated = accounting.quarantined.add(lease.reserved)
        held_slots = integer(accounting.held_slots - lease.reserved_slots)
        quarantined_slots = integer(accounting.quarantined_slots + lease.reserved_slots)
        first = accounting.quota_state == "held"
        if not first and accounting.quarantined_attempts < 1:
            raise ValueError("Missing quarantined quota")
        held_attempts = integer(accounting.held_attempts - int(first))
        quarantined_attempts = integer(accounting.quarantined_attempts + int(first))
        versions = tuple(
            integer(value + increment, minimum=1, maximum=MAX_VERSION)
            for value, increment in (
                (accounting.host_version, 1),
                (accounting.instance_version, 1),
                (accounting.attempt_version, int(first)),
                (accounting.quota_version, int(first)),
                (lease.version, 1),
            )
        )
    except ValueError:
        raise LeaseRejectedError("reconciliation_required") from None
    above_threshold = any(
        _above_threshold(used, capacity, accounting.threshold_ppm)
        for used, capacity in zip(isolated.values, accounting.capacity.values, strict=True)
    )
    return QuarantineDecision(
        replace(
            lease,
            lease_state="quarantined",
            cleanup_state="quarantined",
            version=versions[4],
            observed_at_us=request.observed_at_us,
            reason_code=request.reason_code,
        ),
        replace(
            accounting,
            held=held,
            quarantined=isolated,
            node_state=(
                "quarantined"
                if above_threshold and accounting.node_state == "active"
                else accounting.node_state
            ),
            held_slots=held_slots,
            quarantined_slots=quarantined_slots,
            held_attempts=held_attempts,
            quarantined_attempts=quarantined_attempts,
            quota_state="quarantined",
            host_version=versions[0],
            instance_version=versions[1],
            attempt_version=versions[2],
            quota_version=versions[3],
        ),
        False,
    )


@dataclass(frozen=True, slots=True)
class LeaseResult:
    outcome: Literal["committed", "replayed", "conflict", "rejected", "unknown"]
    lease_id: str
    fact: LeaseRecord | None = None
    error_code: str | None = None
    recovery: Literal["none", "retry_transaction", "query_original", "reconcile"] = "none"

    def __post_init__(self) -> None:
        if not isinstance(self.lease_id, str) or not IDENTIFIER.fullmatch(self.lease_id):
            raise ValueError("Invalid lease result identity")
        if self.outcome in ("committed", "replayed"):
            if (
                self.fact is None
                or self.fact.lease_id != self.lease_id
                or self.error_code is not None
                or self.recovery != "none"
            ):
                raise ValueError("Lease success requires the original snapshot")
        elif self.fact is not None or not self.error_code:
            raise ValueError("Unresolved lease result cannot expose success")
        if self.outcome == "unknown" and (
            self.error_code != "outcome_unknown" or self.recovery != "query_original"
        ):
            raise ValueError("Unknown lease commit requires an original identity query")


def rejected(lease_id: str, code: str) -> LeaseResult:
    return LeaseResult(
        "conflict" if code == "idempotency_conflict" else "rejected",
        lease_id,
        error_code=code,
        recovery="reconcile",
    )


def unknown(lease_id: str) -> LeaseResult:
    return LeaseResult("unknown", lease_id, error_code="outcome_unknown", recovery="query_original")
