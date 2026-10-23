"""Bounded PostgreSQL Job transactions with explicit commit-outcome handling."""

from __future__ import annotations

import random
import time
from typing import cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.jobs import CreateJob, JobResult, unknown_result
from speechnexus_backend.speech.infrastructure.jobs_sql import (
    JobCursor,
    create_in_transaction,
    recover_in_transaction,
)

# Compatibility imports for the first Job unit; pool policy is shared.
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection as JobConnection,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechPool as JobPool,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    speech_pool as job_pool,
)

__all__ = ["JobConnection", "JobPool", "PostgresJobTransactions", "job_pool"]


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
