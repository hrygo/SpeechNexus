"""PostgreSQL schedule for bounded public Job cancellation reconciliation."""

from __future__ import annotations

from typing import Any, Protocol, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    CancellationCandidate,
    ReconciliationOutcome,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    RECONCILIATION_TABLE,
    TABLE,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_MAX_TIMESTAMP_US = 253_402_300_799_999_999


class ReconciliationStoreUnavailableError(RuntimeError):
    """The reconciliation schedule could not be read or committed safely."""


class _Rows(Protocol):
    def fetchall(self) -> list[dict[str, Any]]: ...


class _Cursor(Protocol):
    def execute(self, query: str, parameters: object = ()) -> _Rows: ...


_CLAIM = f"""
WITH due AS (
    SELECT f.job_id
    FROM {TABLE} AS f
    LEFT JOIN {RECONCILIATION_TABLE} AS r ON r.job_id = f.job_id
    WHERE f.accepted_at_us <= %(accepted_before_us)s
      AND (
        r.job_id IS NULL
        OR (
            r.settled_at_us IS NULL
            AND r.last_outcome IS DISTINCT FROM 'exhausted'
            AND r.next_check_at_us <= %(now_us)s
        )
    )
    ORDER BY f.accepted_at_us, f.job_id
    LIMIT %(limit)s
), claimed AS (
    INSERT INTO {RECONCILIATION_TABLE}
        (job_id, attempts, next_check_at_us, updated_at_us)
    SELECT job_id, 1, %(lease_until_us)s, %(now_us)s FROM due
    ON CONFLICT (job_id) DO UPDATE SET
        attempts = {RECONCILIATION_TABLE}.attempts + 1,
        next_check_at_us = EXCLUDED.next_check_at_us,
        updated_at_us = EXCLUDED.updated_at_us
    WHERE {RECONCILIATION_TABLE}.settled_at_us IS NULL
      AND {RECONCILIATION_TABLE}.last_outcome IS DISTINCT FROM 'exhausted'
      AND {RECONCILIATION_TABLE}.next_check_at_us <= %(now_us)s
    RETURNING job_id, attempts, next_check_at_us
)
SELECT c.job_id, c.attempts, c.next_check_at_us, f.accepted_at_us
FROM claimed AS c
JOIN {TABLE} AS f ON f.job_id = c.job_id
ORDER BY f.accepted_at_us, c.job_id
"""

_RECORD_SETTLED = (
    f"UPDATE {RECONCILIATION_TABLE} SET last_outcome = 'settled', settled_at_us = %s, "
    "next_check_at_us = %s, updated_at_us = %s "
    "WHERE job_id = %s AND settled_at_us IS NULL"
)
_RECORD_RETRY = (
    f"UPDATE {RECONCILIATION_TABLE} SET last_outcome = %s, next_check_at_us = %s, "
    "updated_at_us = %s WHERE job_id = %s AND settled_at_us IS NULL"
)
_RECORD_EXHAUSTED = (
    f"UPDATE {RECONCILIATION_TABLE} SET last_outcome = %s, next_check_at_us = %s, "
    "updated_at_us = %s WHERE job_id = %s AND settled_at_us IS NULL"
)
_READ = (
    f"SELECT job_id, attempts, next_check_at_us, last_outcome, settled_at_us "
    f"FROM {RECONCILIATION_TABLE} WHERE job_id = %s"
)


def _check_time(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_TIMESTAMP_US:
        raise ValueError(f"Invalid {name} timestamp")
    return value


class PostgresCancellationReconciliationStore:
    """Short claim and outcome transactions; no engine or network work inside them."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Cancellation reconciliation requires bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def _run(self, work: Any) -> Any:
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                return work(connection)
        except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as error:
            raise ReconciliationStoreUnavailableError(
                "Cancellation reconciliation storage is unavailable"
            ) from error

    def claim_due(
        self,
        *,
        now_us: int,
        limit: int,
        lease_until_us: int,
        accepted_before_us: int,
    ) -> list[CancellationCandidate]:
        _check_time(now_us, "claim")
        _check_time(lease_until_us, "lease")
        _check_time(accepted_before_us, "minimum age")
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("Invalid cancellation reconciliation batch")

        def work(cursor: _Cursor) -> list[CancellationCandidate]:
            rows: list[dict[str, Any]] = cursor.execute(
                _CLAIM,
                {
                    "now_us": now_us,
                    "limit": limit,
                    "lease_until_us": lease_until_us,
                    "accepted_before_us": accepted_before_us,
                },
            ).fetchall()
            return [
                CancellationCandidate(
                    job_id=row["job_id"],
                    accepted_at_us=int(row["accepted_at_us"]),
                    attempts=int(row["attempts"]),
                    next_check_at_us=int(row["next_check_at_us"]),
                )
                for row in rows
            ]

        return cast(list[CancellationCandidate], self._run(work))

    def record_settled(self, job_id: str, *, now_us: int) -> None:
        _check_time(now_us, "settlement")

        def work(cursor: _Cursor) -> None:
            cursor.execute(_RECORD_SETTLED, (now_us, now_us, now_us, job_id))

        self._run(work)

    def record_retry(
        self,
        job_id: str,
        *,
        now_us: int,
        outcome: ReconciliationOutcome,
        next_check_at_us: int,
    ) -> None:
        _check_time(now_us, "retry")
        _check_time(next_check_at_us, "next check")
        if outcome not in ("pending", "stop_unconfirmed", "unavailable"):
            raise ValueError("Invalid retry outcome")

        def work(cursor: _Cursor) -> None:
            cursor.execute(_RECORD_RETRY, (outcome, next_check_at_us, now_us, job_id))

        self._run(work)

    def record_exhausted(self, job_id: str, *, now_us: int, outcome: ReconciliationOutcome) -> None:
        _check_time(now_us, "exhaustion")
        if outcome not in ("terminal_unconfirmed", "exhausted"):
            raise ValueError("Invalid exhaustion outcome")

        def work(cursor: _Cursor) -> None:
            cursor.execute(_RECORD_EXHAUSTED, (outcome, now_us, now_us, job_id))

        self._run(work)

    def read(self, job_id: str) -> dict[str, Any] | None:
        def work(cursor: _Cursor) -> dict[str, Any] | None:
            rows: list[dict[str, Any]] = cursor.execute(_READ, (job_id,)).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise ReconciliationStoreUnavailableError(
                    "Cancellation reconciliation row is not unique"
                )
            return rows[0]

        return cast(dict[str, Any] | None, self._run(work))


__all__ = ["PostgresCancellationReconciliationStore", "ReconciliationStoreUnavailableError"]
