"""Immutable creation identities and replay decisions, independent of storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class CreateJob:
    """Trusted canonical business digest and immutable coordination identities.

    request_digest is computed by the calling business use case after defaults
    and revision pins are resolved. It is not a digest of itself or this wrapper.
    This first unit persists coordination metadata, not a public Job payload.
    """

    job_id: str
    operation_id: str
    attempt_id: str
    event_id: str
    event_digest: str
    request_digest: str

    def __post_init__(self) -> None:
        for value in (self.job_id, self.operation_id, self.attempt_id, self.event_id):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError("Invalid job identity")
        for value in (self.event_digest, self.request_digest):
            if not isinstance(value, str) or not _DIGEST.fullmatch(value):
                raise ValueError("Invalid job digest")


@dataclass(frozen=True, slots=True)
class JobFact:
    job_id: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class JobResult:
    outcome: Literal["committed", "replayed", "conflict", "rejected", "unknown"]
    job_id: str
    fact: JobFact | None = None
    error_code: str | None = None
    recovery: Literal["none", "retry_transaction", "query_original", "reconcile"] = "none"

    def __post_init__(self) -> None:
        if self.outcome in ("committed", "replayed"):
            if (
                self.fact is None
                or self.fact.job_id != self.job_id
                or self.error_code is not None
                or self.recovery != "none"
            ):
                raise ValueError("Successful creation requires its original Job fact")
        elif self.fact is not None or not self.error_code:
            raise ValueError("An unresolved creation cannot expose a committed fact")
        if self.outcome == "unknown" and (
            self.error_code != "outcome_unknown" or self.recovery != "query_original"
        ):
            raise ValueError("Unknown creation can only query its original identity")


def replay_result(request: CreateJob, existing: CreateJob) -> JobResult:
    """Every identity and both digests are immutable, even on a business-key hit."""
    if request != existing:
        return JobResult("conflict", request.job_id, error_code="idempotency_conflict")
    return JobResult("replayed", existing.job_id, JobFact(existing.job_id, existing.request_digest))


def unknown_result(request: CreateJob) -> JobResult:
    return JobResult(
        "unknown", request.job_id, error_code="outcome_unknown", recovery="query_original"
    )
