"""Bounded PostgreSQL Job transactions with explicit commit-outcome handling."""

from __future__ import annotations

import random
import re
import time
from typing import Any, cast

import psycopg
from psycopg import IsolationLevel
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.jobs import CreateJob, JobResult, unknown_result
from speechnexus_backend.speech.infrastructure.jobs_sql import (
    JobCursor,
    create_in_transaction,
    recover_in_transaction,
)

type JobConnection = psycopg.Connection[dict[str, Any]]
type JobPool = ConnectionPool[JobConnection]


def job_pool(
    database_url: str, *, schema: str = "speech", max_size: int = 4, max_waiting: int = 16
) -> JobPool:
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
        raise ValueError("Invalid speech schema")
    if not 1 <= max_size <= 64 or not 1 <= max_waiting <= 1024:
        raise ValueError("Job pool must be bounded")

    def configure(connection: JobConnection) -> None:
        connection.isolation_level = IsolationLevel.READ_COMMITTED
        connection.execute("SELECT set_config('search_path', %s, false)", (schema + ",pg_catalog",))
        connection.execute("SET TIME ZONE 'UTC'")
        connection.commit()

    return ConnectionPool[JobConnection](
        database_url,
        kwargs={"row_factory": dict_row, "connect_timeout": 3},
        configure=configure,
        name="speech-jobs",
        min_size=0,
        max_size=max_size,
        max_waiting=max_waiting,
        timeout=3,
        open=False,
    )


class PostgresJobTransactions:
    def __init__(self, pool: JobPool, *, timeout_seconds: float = 3, max_attempts: int = 3) -> None:
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Job transactions need bounded deadlines and retries")
        self._pool = pool
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts

    def _transaction(
        self,
        request: CreateJob,
        *,
        recover: bool,
        deadline: float | None = None,
        conflict_if_absent: bool = False,
    ) -> JobResult:
        if deadline is None:
            deadline = time.monotonic() + self._timeout
        for attempt in range(self._max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with self._pool.connection(timeout=remaining) as connection:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return JobResult(
                            "rejected",
                            request.job_id,
                            error_code="reconciliation_required",
                            recovery="retry_transaction",
                        )
                    if recover:
                        connection.execute("SET TRANSACTION READ ONLY")
                    connection.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(max(1, int(remaining * 1000))),),
                    )
                    with connection.cursor() as cursor:
                        if recover:
                            result = recover_in_transaction(
                                cast(JobCursor, cursor),
                                request,
                                conflict_if_absent=conflict_if_absent,
                            )
                        else:
                            result = create_in_transaction(cast(JobCursor, cursor), request)
                return result
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                # These SQLSTATEs confirm abort. The context has rolled back and
                # released the connection before any jitter or whole-txn retry.
                delay = min(random.uniform(0.001, 0.01), max(0, deadline - time.monotonic()))
                if delay and attempt + 1 < self._max_attempts:
                    time.sleep(delay)
            except psycopg.errors.UniqueViolation:
                # An insertion race lost a unique key. The entire transaction was
                # rolled back, so resolving the winner is read-only and safe.
                if recover:
                    return unknown_result(request)
                return self._transaction(
                    request, recover=True, deadline=deadline, conflict_if_absent=True
                )
            except PoolClosed, PoolTimeout, TooManyRequests:
                break  # No connection was acquired, so no transaction was attempted.
            except psycopg.OperationalError, psycopg.InterfaceError:
                # Includes lost commit acknowledgments. Never retry the write.
                return unknown_result(request)
            except psycopg.Error:
                return JobResult(
                    "rejected",
                    request.job_id,
                    error_code="reconciliation_required",
                    recovery="reconcile",
                )
        return JobResult(
            "rejected",
            request.job_id,
            error_code="reconciliation_required",
            recovery="retry_transaction",
        )

    def create_job(self, request: CreateJob) -> JobResult:
        return self._transaction(request, recover=False)

    def recover_create_job(self, request: CreateJob) -> JobResult:
        return self._transaction(request, recover=True)
