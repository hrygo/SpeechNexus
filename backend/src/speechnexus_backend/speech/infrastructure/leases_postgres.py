"""Bounded PostgreSQL isolation transactions and read-only unknown recovery."""

from __future__ import annotations

import random
import time
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.domain.leases import (
    LeaseRejectedError,
    LeaseResult,
    QuarantineLease,
    rejected,
    unknown,
)
from speechnexus_backend.speech.domain.runtime_intents import identifier
from speechnexus_backend.speech.infrastructure.leases_sql import (
    LeaseCursor,
    quarantine_in_transaction,
    query_in_transaction,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class _BudgetExpiredError(Exception):
    """The enclosing transaction must roll back before attempting commit."""


class _DeadlineCursor:
    def __init__(self, cursor: LeaseCursor, deadline: float) -> None:
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
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self._cursor.execute(query, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._cursor.fetchall()

    def clock(self) -> int:
        self.execute(
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        )
        return cast(int, self.fetchall()[0]["now_us"])


class PostgresLeaseTransactions:
    def __init__(
        self, pool: SpeechPool, *, timeout_seconds: float = 3, max_attempts: int = 3
    ) -> None:
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Lease transactions require bounded deadlines and retries")
        self._pool = pool
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts

    def _run(self, request: QuarantineLease | str) -> LeaseResult:
        lease_id = request if isinstance(request, str) else request.lease_id
        identifier(lease_id)
        if not isinstance(request, str):
            validate_coordination(
                "quarantine_lease",
                {
                    "lease_id": request.lease_id,
                    "reason_code": request.reason_code,
                    "observed_at_us": request.observed_at_us,
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
                        result = (
                            query_in_transaction(cursor, request)
                            if isinstance(request, str)
                            else quarantine_in_transaction(cursor, request, clock=cursor.clock)
                        )
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
