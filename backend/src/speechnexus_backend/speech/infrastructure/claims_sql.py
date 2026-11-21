"""Short claim row transactions; no execution, I/O or capacity mutation."""

from collections.abc import Callable
from typing import Any, Protocol

from speechnexus_backend.speech.domain.claims import (
    ClaimJob,
    ClaimRejectedError,
    ClaimResult,
    JobClaim,
    claimed,
    identifier,
    unknown,
)


class ClaimCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def read_claim(cursor: ClaimCursor, job_id: str, *, lock: bool) -> JobClaim:
    identifier(job_id)
    cursor.execute(
        "SELECT job_id, claim_owner_id AS worker_id, claim_version, "
        "claim_expires_at_us AS expires_at_us, job_state FROM coordination_jobs "
        "WHERE job_id = %s" + (" FOR UPDATE" if lock else ""),
        (job_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise ClaimRejectedError("not_found")
    if len(rows) != 1:
        raise ClaimRejectedError("reconciliation_required")
    try:
        return JobClaim(**rows[0])
    except ValueError, TypeError:
        raise ClaimRejectedError("reconciliation_required") from None


def query_in_transaction(cursor: ClaimCursor, job_id: str) -> ClaimResult:
    fact = read_claim(cursor, job_id, lock=False)
    if fact.worker_id is None:
        return unknown(job_id)
    # A current projection, possibly expired or owned by a later worker. This
    # does not prove an earlier claim rolled back and does not permit dispatch.
    return ClaimResult("replayed", job_id, fact)


def claim_in_transaction(
    cursor: ClaimCursor, request: ClaimJob, *, clock: Callable[[], int]
) -> ClaimResult:
    current = read_claim(cursor, request.job_id, lock=True)
    now_us = clock()  # Database clock after any candidate-row lock wait.
    updated = claimed(current, request, now_us)
    cursor.execute(
        "UPDATE coordination_jobs SET claim_owner_id = %s, claim_version = %s, "
        "claim_expires_at_us = %s WHERE job_id = %s AND claim_version = %s "
        "AND claim_expires_at_us <= %s AND job_state = 'pending'",
        (
            updated.worker_id,
            updated.claim_version,
            updated.expires_at_us,
            request.job_id,
            current.claim_version,
            now_us,
        ),
    )
    if cursor.rowcount != 1:
        raise ClaimRejectedError("reconciliation_required")
    return ClaimResult("committed", request.job_id, updated)
