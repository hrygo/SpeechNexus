"""Bounded PostgreSQL claims; ambiguous commits require a read-only query."""

from __future__ import annotations

import random
import time
from typing import Any, cast

from speechnexus_backend.speech.domain.claims import (
    ClaimJob,
    ClaimRejectedError,
    ClaimResult,
    identifier,
    rejected,
    unknown,
)
from speechnexus_backend.speech.infrastructure.claims_sql import (
    ClaimCursor,
    claim_in_transaction,
    query_in_transaction,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_portable_sql.clock import ClockReader, now_us
from speechnexus_portable_sql.transaction import is_savepoint_control
from speechnexus_portable_sql.vendor import PoolClosed, PoolTimeout, TooManyRequests, psycopg


class _BudgetExpiredError(Exception):
    """Rollback before commit when the shared request budget is exhausted."""


class _DeadlineCursor:
    def __init__(self, cursor: ClaimCursor, deadline: float) -> None:
        self._cursor = cursor
        self._deadline = deadline

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _BudgetExpiredError
        if is_savepoint_control(query):
            return self._cursor.execute(query, parameters)
        self._cursor.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self._cursor.execute(query, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._cursor.fetchall()

    def fetchone(self) -> dict[str, Any] | None:
        rows = self.fetchall()
        return rows[0] if rows else None

    def clock(self) -> int:
        # 「现在」只从便携时钟读一次;语句里不再有品牌函数。
        now: int = now_us(cast(ClockReader, self))
        return now


class PostgresClaimTransactions:
    def __init__(
        self, pool: SpeechPool, *, timeout_seconds: float = 3, max_attempts: int = 3
    ) -> None:
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Claim transactions need bounded deadlines and retries")
        self._pool = pool
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts

    def _run(self, request: ClaimJob | str) -> ClaimResult:
        job_id = request if isinstance(request, str) else request.job_id
        identifier(job_id)
        deadline = time.monotonic() + self._timeout
        for attempt in range(self._max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with self._pool.connection(timeout=remaining) as connection:
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
                    if isinstance(request, str):
                        connection.execute("SET TRANSACTION READ ONLY")
                    with connection.cursor() as raw:
                        cursor = _DeadlineCursor(cast(ClaimCursor, raw), deadline)
                        result = (
                            query_in_transaction(cursor, request)
                            if isinstance(request, str)
                            else claim_in_transaction(cursor, request, clock=cursor.clock)
                        )
                        if time.monotonic() >= deadline:
                            raise _BudgetExpiredError
                return result  # Includes commit acknowledgment, never before it.
            except ClaimRejectedError as error:
                return rejected(job_id, error.code)
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                # The context has confirmed rollback and released the connection.
                delay = min(random.uniform(0.001, 0.01), max(0, deadline - time.monotonic()))
                if delay and attempt + 1 < self._max_attempts:
                    time.sleep(delay)
            except PoolClosed, PoolTimeout, TooManyRequests, _BudgetExpiredError:
                break
            except psycopg.OperationalError, psycopg.InterfaceError:
                return unknown(job_id)  # No automatic write retry or fresh claim.
            except psycopg.Error:
                return rejected(job_id, "reconciliation_required")
        return ClaimResult(
            "rejected", job_id, error_code="reconciliation_required", recovery="retry_transaction"
        )

    def claim_job(self, request: ClaimJob) -> ClaimResult:
        return self._run(request)

    def query_claim(self, job_id: str) -> ClaimResult:
        return self._run(job_id)
