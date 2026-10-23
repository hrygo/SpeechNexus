"""Current Job claim authority, independent of execution or capacity leases."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

MAX_COUNTER = 2**53 - 1
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")


def identifier(value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid claim identity")


def counter(value: int, *, minimum: int = 0, maximum: int = MAX_COUNTER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Invalid claim counter")
    return value


@dataclass(frozen=True, slots=True)
class ClaimJob:
    job_id: str
    worker_id: str
    expected_claim_version: int
    ttl_ms: int

    def __post_init__(self) -> None:
        identifier(self.job_id)
        identifier(self.worker_id)
        counter(self.expected_claim_version)
        counter(self.ttl_ms, minimum=1, maximum=60000)


@dataclass(frozen=True, slots=True)
class JobClaim:
    job_id: str
    worker_id: str | None
    claim_version: int
    expires_at_us: int
    job_state: str

    def __post_init__(self) -> None:
        identifier(self.job_id)
        counter(self.claim_version)
        counter(self.expires_at_us)
        if self.worker_id is None:
            if self.claim_version or self.expires_at_us:
                raise ValueError("Unclaimed Job has no prior authority")
        else:
            identifier(self.worker_id)
            counter(self.claim_version, minimum=1)
            counter(self.expires_at_us, minimum=1)
        if not isinstance(self.job_state, str) or not self.job_state:
            raise ValueError("Invalid Job state")


class ClaimRejectedError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def claimed(current: JobClaim, request: ClaimJob, now_us: int) -> JobClaim:
    try:
        counter(now_us)
    except ValueError:
        raise ClaimRejectedError("invalid_counter") from None
    if current.job_id != request.job_id:
        raise ClaimRejectedError("reconciliation_required")
    if (
        current.job_state != "pending"
        or current.claim_version != request.expected_claim_version
        or current.expires_at_us > now_us
    ):
        raise ClaimRejectedError("version_conflict")
    try:
        expires_at_us = counter(now_us + request.ttl_ms * 1000, minimum=1)
        version = counter(current.claim_version + 1, minimum=1)
    except ValueError:
        raise ClaimRejectedError("invalid_counter") from None
    return replace(
        current, worker_id=request.worker_id, claim_version=version, expires_at_us=expires_at_us
    )


@dataclass(frozen=True, slots=True)
class ClaimResult:
    outcome: Literal["committed", "replayed", "conflict", "rejected", "unknown"]
    job_id: str
    fact: JobClaim | None = None
    error_code: str | None = None
    recovery: Literal["none", "retry_transaction", "query_current_authority", "reconcile"] = "none"

    def __post_init__(self) -> None:
        identifier(self.job_id)
        if self.outcome in ("committed", "replayed"):
            if (
                self.fact is None
                or self.fact.job_id != self.job_id
                or self.fact.worker_id is None
                or self.error_code is not None
                or self.recovery != "none"
            ):
                raise ValueError("Claim success requires a complete current authority")
        elif self.fact is not None or not self.error_code:
            raise ValueError("Unresolved claim cannot expose success")
        if self.outcome == "unknown" and (
            self.error_code != "outcome_unknown" or self.recovery != "query_current_authority"
        ):
            raise ValueError("Unknown claim requires a current-authority query")


def rejected(job_id: str, code: str) -> ClaimResult:
    conflict = code == "version_conflict"
    return ClaimResult(
        "conflict" if conflict else "rejected",
        job_id,
        error_code=code,
        recovery="query_current_authority" if conflict else "reconcile",
    )


def unknown(job_id: str) -> ClaimResult:
    return ClaimResult(
        "unknown", job_id, error_code="outcome_unknown", recovery="query_current_authority"
    )
