"""Bounded owner transactions with current-authority recovery after ambiguity."""

from __future__ import annotations

import random
import time
from typing import Any, cast

from speechnexus_backend.speech.domain.owners import (
    POOLS,
    AcquireOwner,
    ActivateOwner,
    OwnerRejectedError,
    OwnerResult,
    RenewOwner,
    reject,
    unknown,
)
from speechnexus_backend.speech.infrastructure.owners_sql import (
    OwnerCursor,
    mutate_in_transaction,
    query_in_transaction,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_portable_sql.clock import ClockReader, now_us
from speechnexus_portable_sql.vendor import PoolClosed, PoolTimeout, TooManyRequests, psycopg


class _BudgetExpiredError(Exception):
    """Raised before commit; the connection context rolls back any changes."""


class _DeadlineCursor:
    def __init__(self, cursor: OwnerCursor, deadline: float) -> None:
        self._cursor = cursor
        self._deadline = deadline

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _BudgetExpiredError
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


class PostgresOwnerTransactions:
    def __init__(
        self, pool: SpeechPool, *, timeout_seconds: float = 3, max_attempts: int = 3
    ) -> None:
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Owner transactions need bounded deadlines and retries")
        self._pool = pool
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts

    def _run(self, request: AcquireOwner | RenewOwner | ActivateOwner | str) -> OwnerResult:
        if isinstance(request, str):
            pool_id = request
        elif isinstance(request, AcquireOwner):
            pool_id = request.pool_id
        else:
            pool_id = request.authority.pool_id
        if pool_id not in POOLS:
            raise ValueError("Invalid owner pool")
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
                        cursor = _DeadlineCursor(cast(OwnerCursor, raw), deadline)
                        result = (
                            query_in_transaction(cursor, request)
                            if isinstance(request, str)
                            else mutate_in_transaction(cursor, request, clock=cursor.clock)
                        )
                        if time.monotonic() >= deadline:
                            raise _BudgetExpiredError
                return result  # Commit acknowledgment was received.
            except OwnerRejectedError as error:
                return reject(pool_id, error.code)
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                # Confirmed abort only. No connection/transaction is held here.
                delay = min(random.uniform(0.001, 0.01), max(0, deadline - time.monotonic()))
                if delay and attempt + 1 < self._max_attempts:
                    time.sleep(delay)
            except PoolClosed, PoolTimeout, TooManyRequests, _BudgetExpiredError:
                break
            except psycopg.OperationalError, psycopg.InterfaceError:
                return unknown(pool_id)  # Never automatically acquire or renew again.
            except psycopg.Error:
                return reject(pool_id, "reconciliation_required")
        return OwnerResult(
            "rejected", pool_id, error_code="reconciliation_required", recovery="retry_transaction"
        )

    def acquire_owner(self, request: AcquireOwner) -> OwnerResult:
        return self._run(request)

    def renew_owner(self, request: RenewOwner) -> OwnerResult:
        return self._run(request)

    def activate_owner(self, request: ActivateOwner) -> OwnerResult:
        return self._run(request)

    def query_owner(self, pool_id: str) -> OwnerResult:
        return self._run(pool_id)
