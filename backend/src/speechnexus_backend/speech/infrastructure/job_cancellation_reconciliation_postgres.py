"""PostgreSQL schedule for bounded public Job cancellation reconciliation."""

from __future__ import annotations

from typing import Any, Protocol, cast

from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    CancellationCandidate,
    ReconciliationOutcome,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    ATTEMPT_CEILING,
    NOT_SUBMITTED_TABLE,
    RECONCILIATION_TABLE,
    TABLE,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_portable_sql.transaction import insert_if_absent
from speechnexus_portable_sql.vendor import PoolClosed, PoolTimeout, TooManyRequests, psycopg

_MAX_TIMESTAMP_US = 253_402_300_799_999_999
# The schema's ``attempts`` CHECK bound, imported rather than restated: the DDL and this
# claim clamp must be one number. A local literal would let them drift silently --- a claim
# bound below the CHECK never lets the read face name a spent row, one above it makes the
# claim ``UPDATE`` violate the CHECK and the row stops being retried at all.
_MAX_ATTEMPTS = ATTEMPT_CEILING
# keyset 游标的下界:时间戳列是非负整数,所以 -1 严格小于任何真实值。
_KEYSET_START = -1


class ReconciliationStoreUnavailableError(RuntimeError):
    """The reconciliation schedule could not be read or committed safely."""


class _Rows(Protocol):
    rowcount: int

    def fetchall(self) -> list[dict[str, Any]]: ...
    def fetchmany(self, size: int) -> list[dict[str, Any]]: ...


class _Cursor(Protocol):
    def execute(self, query: str, parameters: object = ()) -> _Rows: ...


_CANDIDATES = f"""
SELECT f.job_id, f.accepted_at_us, n.observed_at_us AS not_submitted_at_us
FROM {TABLE} AS f
LEFT JOIN {RECONCILIATION_TABLE} AS r ON r.job_id = f.job_id
LEFT JOIN {NOT_SUBMITTED_TABLE} AS n ON n.job_id = f.job_id
WHERE f.accepted_at_us <= ?
  AND (f.accepted_at_us > ? OR (f.accepted_at_us = ? AND f.job_id > ?))
  AND (
    r.job_id IS NULL
    OR (r.settled_at_us IS NULL
        AND (r.last_outcome IS NULL OR r.last_outcome <> 'exhausted')
        AND r.next_check_at_us <= ?)
  )
ORDER BY f.accepted_at_us, f.job_id
"""
# 抢占的两步:先读,再按读到的 ``attempts`` 做 CAS 更新;行不存在时插入。
# 受限方言没有 ``WITH`` / ``LIMIT`` / ``RETURNING``,也没有 ``ON CONFLICT``。
_READ_CLAIM = f"""
SELECT attempts, settled_at_us, last_outcome, next_check_at_us
FROM {RECONCILIATION_TABLE} WHERE job_id = ?
"""
_INSERT_CLAIM = f"""
INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, updated_at_us)
VALUES (?, 1, ?, ?)
"""
_TAKE_CLAIM = f"""
UPDATE {RECONCILIATION_TABLE}
SET attempts = ?, next_check_at_us = ?, updated_at_us = ?
WHERE job_id = ? AND attempts = ? AND settled_at_us IS NULL AND next_check_at_us <= ?
"""

_RECORD_SETTLED = (
    f"UPDATE {RECONCILIATION_TABLE} SET last_outcome = 'settled', settled_at_us = ?, "
    "next_check_at_us = ?, updated_at_us = ? "
    "WHERE job_id = ? AND settled_at_us IS NULL"
)
_RECORD_RETRY = (
    f"UPDATE {RECONCILIATION_TABLE} SET last_outcome = ?, next_check_at_us = ?, "
    "updated_at_us = ? WHERE job_id = ? AND settled_at_us IS NULL"
)
_RECORD_EXHAUSTED = (
    f"UPDATE {RECONCILIATION_TABLE} SET last_outcome = ?, next_check_at_us = ?, "
    "updated_at_us = ? WHERE job_id = ? AND settled_at_us IS NULL"
)
_READ = (
    f"SELECT job_id, attempts, next_check_at_us, last_outcome, settled_at_us "
    f"FROM {RECONCILIATION_TABLE} WHERE job_id = ?"
)


def _take(cursor: _Cursor, job_id: str, *, now_us: int, lease_until_us: int) -> int | None:
    """抢占一条到期账本行,返回这次的 ``attempts``;不满足条件时返回 ``None``.

    ``attempts`` 按上限饱和而不是中止抢占:账本是排程,不是放弃计数。CAS 的判据只有
    ``rowcount`` —— 并发抢占者之一会拿到 0,于是安静地让出。
    """

    stored = cursor.execute(_READ_CLAIM, (job_id,)).fetchall()
    if not stored:
        if insert_if_absent(cursor, _INSERT_CLAIM, (job_id, lease_until_us, now_us)):
            return 1
        # 另一个抢占者刚插入:退回 CAS 更新,由下面的判据决定归属。
        stored = cursor.execute(_READ_CLAIM, (job_id,)).fetchall()
        if not stored:
            return None
    row = stored[0]
    if row["settled_at_us"] is not None or row["last_outcome"] == "exhausted":
        return None
    if int(row["next_check_at_us"]) > now_us:
        return None
    attempts = int(row["attempts"])
    taken = min(attempts + 1, _MAX_ATTEMPTS)
    # ``rowcount`` 来自 ``execute`` 返回的游标:池里传进来的既可能是连接也可能是游标。
    updated = cursor.execute(
        _TAKE_CLAIM,
        (taken, lease_until_us, now_us, job_id, attempts, now_us),
    )
    return taken if updated.rowcount == 1 else None


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
                    "SELECT set_config('statement_timeout', ?, true)",
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
            # 候选按 ``(accepted_at_us, job_id)`` 的 keyset 游标前进:每次只取 ``limit`` 行,
            # 因此不需要 ``LIMIT``,也不会把整个到期集合读进内存。
            rows = cursor.execute(
                _CANDIDATES,
                (accepted_before_us, _KEYSET_START, _KEYSET_START, "", now_us),
            ).fetchmany(limit)
            claimed: list[CancellationCandidate] = []
            for row in rows:
                attempts = _take(
                    cursor,
                    str(row["job_id"]),
                    now_us=now_us,
                    lease_until_us=lease_until_us,
                )
                if attempts is None:
                    continue
                claimed.append(
                    CancellationCandidate(
                        job_id=str(row["job_id"]),
                        accepted_at_us=int(row["accepted_at_us"]),
                        attempts=attempts,
                        next_check_at_us=lease_until_us,
                        not_submitted_at_us=(
                            None
                            if row["not_submitted_at_us"] is None
                            else int(row["not_submitted_at_us"])
                        ),
                    )
                )
            return claimed

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
