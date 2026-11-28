"""Bounded PostgreSQL lease transactions and read-only unknown recovery."""

from __future__ import annotations

import random
import time
from typing import Any, Protocol, cast

from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import (
    LeaseRejectedError,
    LeaseResult,
    QuarantineLease,
    rejected,
    unknown,
)
from speechnexus_backend.speech.domain.runtime_intents import identifier
from speechnexus_backend.speech.infrastructure.lease_release_sql import release_in_transaction
from speechnexus_backend.speech.infrastructure.leases_sql import (
    LeaseCursor,
    quarantine_in_transaction,
    query_in_transaction,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_portable_sql.clock import ClockReader, now_us
from speechnexus_portable_sql.transaction import is_savepoint_control
from speechnexus_portable_sql.vendor import PoolClosed, PoolTimeout, TooManyRequests, psycopg


class _BudgetExpiredError(Exception):
    """The enclosing transaction must roll back before attempting commit."""


class _Closable(Protocol):
    """驱动游标才能关:``LeaseCursor`` 只说「能执行、能取行」。"""

    def close(self) -> None: ...


class _DeadlineCursor:
    def __init__(self, cursor: LeaseCursor, deadline: float) -> None:
        self._cursor = cursor
        self._deadline = deadline

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        # 保存点控制先于预算判定:事务被取消或唯一冲突之后,这条语句是唯一还能执行的一条,
        # 而且它不可能等待,所以既不该被预算拦住,也不该先发一条 `set_config`(那会在中止
        # 的事务里直接失败,把整个事务赔进去)。
        if is_savepoint_control(query):
            return self._cursor.execute(query, parameters)
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _BudgetExpiredError
        self._cursor.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self._cursor.execute(query, parameters)

    def close(self) -> None:
        """关掉被包装的游标:键锁路径自己开游标,所以也要自己关(借用型调用方不调它)."""

        cast(_Closable, self._cursor).close()

    def fetchall(self) -> list[dict[str, Any]]:
        return self._cursor.fetchall()

    def fetchone(self) -> dict[str, Any] | None:
        """便携时钟要一行:用本包装已有的 fetchall 取,不新增协议方法。"""

        rows = self.fetchall()
        return rows[0] if rows else None

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        """A bounded read of an already ordered statement.

        ``LIMIT`` is outside the portable subset, so the sweep's bound is applied
        here, after a total order, on the cursor that already enforces the
        transaction's deadline. This is the documented replacement
        (``fetchmany(page_size + 1)``), not a new paging scheme.
        """
        rows: list[dict[str, Any]] = cast(Any, self._cursor).fetchmany(size)
        return rows

    def clock(self) -> int:
        # 「现在」只从便携时钟读一次;语句里不再有品牌函数。
        now: int = now_us(cast(ClockReader, self))
        return now


class PostgresLeaseTransactions:
    def __init__(
        self, pool: SpeechPool, *, timeout_seconds: float = 3, max_attempts: int = 3
    ) -> None:
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Lease transactions require bounded deadlines and retries")
        self._pool = pool
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts

    def _run(self, request: QuarantineLease | ReleaseLease | str) -> LeaseResult:
        lease_id = request if isinstance(request, str) else request.lease_id
        identifier(lease_id)
        if isinstance(request, QuarantineLease):
            validate_coordination(
                "quarantine_lease",
                {
                    "lease_id": request.lease_id,
                    "reason_code": request.reason_code,
                    "observed_at_us": request.observed_at_us,
                },
            )
        elif isinstance(request, ReleaseLease):
            validate_coordination(
                "release_lease",
                {
                    "lease_id": request.lease_id,
                    "expected_version": str(request.expected_version),
                },
            )
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
                        cursor = _DeadlineCursor(cast(LeaseCursor, raw), deadline)
                        if isinstance(request, str):
                            result = query_in_transaction(cursor, request)
                        elif isinstance(request, ReleaseLease):
                            result = release_in_transaction(cursor, request, clock=cursor.clock)
                        else:
                            result = quarantine_in_transaction(cursor, request, clock=cursor.clock)
                        if time.monotonic() >= deadline:
                            raise _BudgetExpiredError
                return result  # Commit acknowledgement precedes success.
            except LeaseRejectedError as error:
                return rejected(lease_id, error.code)
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                delay = min(random.uniform(0.001, 0.01), max(0, deadline - time.monotonic()))
                if delay and attempt + 1 < self._max_attempts:
                    time.sleep(delay)  # Confirmed rollback; no checked-out connection.
            except PoolClosed, PoolTimeout, TooManyRequests, _BudgetExpiredError:
                break
            except psycopg.OperationalError, psycopg.InterfaceError:
                return unknown(lease_id)  # Never repeat an ambiguous write.
            except psycopg.Error, ValueError, TypeError, KeyError:
                return rejected(lease_id, "reconciliation_required")
        return LeaseResult(
            "rejected", lease_id, error_code="reconciliation_required", recovery="retry_transaction"
        )

    def quarantine_lease(self, request: QuarantineLease) -> LeaseResult:
        return self._run(request)

    def query_lease(self, lease_id: str) -> LeaseResult:
        return self._run(lease_id)

    def release_lease(self, request: ReleaseLease) -> LeaseResult:
        return self._run(request)
