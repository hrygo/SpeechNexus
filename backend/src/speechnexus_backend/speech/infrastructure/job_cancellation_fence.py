"""Write and read the durable public Job cancellation fence.

The fence is the gateway-visible projection of an accepted cancellation. The
``speech_job_cancellations`` receipt is the single acceptance fact; writing the
projection must happen in the same transaction that inserts the receipt, after
that transaction has established the Job's current state. These functions never
change a Job state, stop an execution or release capacity.

Acceptance calls write_cancellation_fence with its own cursor so the fence, the
Job transition and the cancellation receipt commit together. This module
deliberately exposes no standalone commit wrapper: a fence outside that
transaction would block submit without a receipt or a reconciliation schedule.

Ordering is a portable row lock on the canonical Job row (``coordination_jobs``),
taken by ``lock_cancellation_fence`` before writing and by the gateway before
reading. The same row is what the dispatch claim CAS-es and what a terminal
transition locks, so the fixed order stays bucket -> Job -> fence/outbox, and a
submit either observes the committed fence or commits before acceptance. This
module never changes a Job state: the acceptance transaction closes the canonical
dispatch disposition next to the receipt, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import TABLE

_MAX_TIMESTAMP_US = 253_402_300_799_999_999


class CancellationFenceUnavailableError(RuntimeError):
    """The fence could not be read or committed with a known outcome."""


@dataclass(frozen=True, slots=True)
class CancellationFence:
    job_id: str
    accepted_at_us: int
    request_id: str


class FenceRows(Protocol):
    def fetchall(self) -> list[dict[str, Any]]: ...


class FenceCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> FenceRows: ...


def _job_identity(job_id: str) -> None:
    if not isinstance(job_id, str) or not job_id.isascii() or not 1 <= len(job_id) <= 128:
        raise ValueError("Invalid cancellation fence Job identity")


def _check(job_id: str, request_id: str, accepted_at_us: int) -> None:
    _job_identity(job_id)
    if (
        not isinstance(request_id, str)
        or not request_id.isascii()
        or not 1 <= len(request_id) <= 128
    ):
        raise ValueError("Invalid cancellation fence request identity")
    if type(accepted_at_us) is not int or not 0 <= accepted_at_us <= _MAX_TIMESTAMP_US:
        raise ValueError("Invalid cancellation fence acceptance time")


def _row(row: dict[str, Any]) -> CancellationFence:
    try:
        return CancellationFence(
            job_id=row["job_id"],
            accepted_at_us=int(row["accepted_at_us"]),
            request_id=row["request_id"],
        )
    except KeyError, TypeError, ValueError:
        raise CancellationFenceUnavailableError("Stored cancellation fence is invalid") from None


def lock_cancellation_fence(cursor: FenceCursor, job_id: str) -> None:
    """Take the canonical Job row lock that orders the fence against submit and claim.

    The gateway locks ``coordination_jobs.job_id`` for the same Job before it reads
    the fence, and the dispatch claim's version/state CAS locks that row as it
    writes. Whichever transaction takes the row first keeps it to commit, so a
    submit either observes the committed fence or commits before acceptance, and a
    claim cannot slip between an accepted cancellation and its receipt. Released by
    commit/rollback.

    A Job without a ``coordination_jobs`` row locks nothing; that is unreachable
    for the production acceptance path, which reads ``operation_id`` from the same
    row and answers ``unknown`` instead of writing a fence. Callers must take this
    lock before any ``speech_jobs`` row lock: terminal transitions and this module
    both hold Job -> public Job, and the reverse order would deadlock.
    """
    _job_identity(job_id)
    cursor.execute("SELECT job_id FROM coordination_jobs WHERE job_id = ? FOR UPDATE", (job_id,))


def read_cancellation_fence(cursor: FenceCursor, job_id: str) -> CancellationFence | None:
    """Read an existing fence without taking the Job row lock (read-only callers)."""
    _check(job_id, "read", 0)
    rows = cursor.execute(
        f"SELECT job_id, accepted_at_us, request_id FROM {TABLE} WHERE job_id = ?",
        (job_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise CancellationFenceUnavailableError("Cancellation fence is not unique")
    return _row(rows[0])


def write_cancellation_fence(
    cursor: FenceCursor,
    *,
    job_id: str,
    request_id: str,
    accepted_at_us: int,
) -> CancellationFence:
    """Insert once under the Job row lock and return the durable first fact.

    A repeated acceptance keeps the original acceptance time and request identity.
    This function does not verify a stop; it only records that cancellation was
    accepted before any execution creation is allowed. It re-takes the Job row lock
    as a safety net for direct callers; production acceptance already holds it from
    the start of its transaction. After it, do not wait on admission_lease or
    runtime_instance authority rows: the gateway locks authority rows before the Job
    row, so waiting there after the fence would form a lock cycle.
    """
    _check(job_id, request_id, accepted_at_us)
    lock_cancellation_fence(cursor, job_id)
    cursor.execute(
        f"INSERT INTO {TABLE} (job_id, accepted_at_us, request_id) "
        "VALUES (?, ?, ?) ON CONFLICT (job_id) DO NOTHING",
        (job_id, accepted_at_us, request_id),
    )
    fence = read_cancellation_fence(cursor, job_id)
    if fence is None:
        raise CancellationFenceUnavailableError("Cancellation fence disappeared after its write")
    return fence


__all__ = [
    "CancellationFence",
    "CancellationFenceUnavailableError",
    "lock_cancellation_fence",
    "read_cancellation_fence",
    "write_cancellation_fence",
]
