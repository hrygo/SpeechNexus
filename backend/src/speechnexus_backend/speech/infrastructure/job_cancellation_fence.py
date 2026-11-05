"""Write and read the durable public Job cancellation fence.

The fence is the gateway-visible projection of an accepted cancellation. The
``speech_job_cancellations`` receipt is the single acceptance fact; writing the
projection must happen in the same transaction that inserts the receipt, after
that transaction has established the Job's current state. These functions never
change a Job state, stop an execution or release capacity.

The standalone store here exists for composition and for the bounded
reconciliation worker. A production acceptance path should call
write_cancellation_fence with its own connection so the fence, the Job
transition and the cancellation receipt commit together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from speechnexus_contracts.cancellation_fence import job_cancellation_fence_lock_key

from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import TABLE
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

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


def _check(job_id: str, request_id: str, accepted_at_us: int) -> None:
    if not isinstance(job_id, str) or not job_id.isascii() or not 1 <= len(job_id) <= 128:
        raise ValueError("Invalid cancellation fence Job identity")
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
    """Take the shared transaction-scoped lock before writing or reading the fence.

    The gateway derives the same key, so a submit either observes the committed
    fence or completes before acceptance. The lock is released by commit/rollback.
    """
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (job_cancellation_fence_lock_key(job_id),))


def read_cancellation_fence(cursor: FenceCursor, job_id: str) -> CancellationFence | None:
    """Read an existing fence without taking the advisory lock (read-only callers)."""
    _check(job_id, "read", 0)
    rows = cursor.execute(
        f"SELECT job_id, accepted_at_us, request_id FROM {TABLE} WHERE job_id = %s",
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
    """Insert once under the fence lock and return the durable first fact.

    A repeated acceptance keeps the original acceptance time and request identity.
    This function does not verify a stop; it only records that cancellation was
    accepted before any execution creation is allowed. Production acceptance must
    call it as the last write in its own transaction: after the fence lock
    is held, do not wait on admission_lease or runtime_instance authority rows, or
    the gateway's authority-then-fence order could form a lock cycle.
    """
    _check(job_id, request_id, accepted_at_us)
    lock_cancellation_fence(cursor, job_id)
    cursor.execute(
        f"INSERT INTO {TABLE} (job_id, accepted_at_us, request_id) "
        "VALUES (%s, %s, %s) ON CONFLICT (job_id) DO NOTHING",
        (job_id, accepted_at_us, request_id),
    )
    fence = read_cancellation_fence(cursor, job_id)
    if fence is None:
        raise CancellationFenceUnavailableError("Cancellation fence disappeared after its write")
    return fence


class PostgresJobCancellationFence:
    """Fence half of acceptance and a bounded read for reconciliation."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Cancellation fences require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def accept(
        self, job_id: str, *, request_id: str, accepted_at_us: int | None = None
    ) -> CancellationFence:
        """Commit one fence in its own transaction.

        A production acceptance path must call write_cancellation_fence inside
        the transaction that changes the Job instead of this wrapper.
        """
        _check(job_id, request_id, 0 if accepted_at_us is None else accepted_at_us)
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                if accepted_at_us is None:
                    row = connection.execute(
                        "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) "
                        "AS now_us"
                    ).fetchone()
                    if row is None:
                        raise CancellationFenceUnavailableError("Database clock is unavailable")
                    accepted_at_us = int(row["now_us"])
                with connection.cursor() as cursor:
                    return write_cancellation_fence(
                        cursor,
                        job_id=job_id,
                        request_id=request_id,
                        accepted_at_us=accepted_at_us,
                    )
        except CancellationFenceUnavailableError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise CancellationFenceUnavailableError(
                "Cancellation fence storage is unavailable"
            ) from None

    def read(self, job_id: str) -> CancellationFence | None:
        _check(job_id, "read", 0)
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                with connection.cursor() as cursor:
                    return read_cancellation_fence(cursor, job_id)
        except CancellationFenceUnavailableError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise CancellationFenceUnavailableError(
                "Cancellation fence storage is unavailable"
            ) from None


__all__ = [
    "CancellationFence",
    "CancellationFenceUnavailableError",
    "PostgresJobCancellationFence",
    "lock_cancellation_fence",
    "read_cancellation_fence",
    "write_cancellation_fence",
]
