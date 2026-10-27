"""PostgreSQL claim lease and public Job transitions for the dispatcher."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.job_dispatch import (
    ClaimedJob,
    DispatchConflictError,
    DispatchJob,
    DispatchProtocolError,
    DispatchUnavailableError,
    identifier,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_JOB_SELECT = (
    "SELECT j.job_id, j.workspace_id, j.kind, j.state, j.status_version, "
    "j.created_at_us, j.request_digest, COALESCE(a.request_document, '') AS request_document "
    "FROM speech_jobs AS j "
    "LEFT JOIN admission_operations AS a ON a.operation_id = j.job_id "
)


def _job(row: dict[str, Any]) -> DispatchJob:
    try:
        return DispatchJob(
            row["job_id"],
            row["workspace_id"],
            row["kind"],
            row["state"],
            int(row["status_version"]),
            int(row["created_at_us"]),
            row["request_digest"],
            row["request_document"],
        )
    except KeyError, TypeError, ValueError:
        raise DispatchProtocolError("Stored Job facts are invalid") from None


class PostgresDispatchQueue:
    """One transaction claims due rows; ``SKIP LOCKED`` prevents double ownership."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Dispatch claims require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    async def claim_due(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]:
        identifier(worker_id)
        if not 1 <= lease_ms <= 60_000 or not 1 <= limit <= 32:
            raise ValueError("Dispatch claim bounds are invalid")
        return await asyncio.to_thread(self._claim, worker_id, lease_ms, limit)

    def _claim(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]:
        query = (
            "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 "
            "AS BIGINT) AS now_us), due AS ("
            "SELECT c.job_id FROM coordination_jobs AS c "
            "JOIN speech_jobs AS j ON j.job_id = c.job_id CROSS JOIN clock "
            "WHERE c.job_state = 'pending' AND c.claim_expires_at_us <= clock.now_us "
            "AND j.state IN ('pending', 'running') "
            "ORDER BY j.created_at_us, c.job_id FOR UPDATE OF c SKIP LOCKED LIMIT %s"
            "), updated AS (UPDATE coordination_jobs AS c "
            "SET claim_owner_id = %s, claim_version = c.claim_version + 1, "
            "claim_expires_at_us = clock.now_us + %s "
            "FROM due, clock WHERE c.job_id = due.job_id "
            "RETURNING c.job_id, c.claim_version, c.claim_expires_at_us"
            ") SELECT u.job_id, u.claim_version, u.claim_expires_at_us, j.workspace_id, "
            "j.kind, j.state, j.status_version, j.created_at_us, j.request_digest, "
            "COALESCE(a.request_document, '') AS request_document "
            "FROM updated AS u JOIN speech_jobs AS j ON j.job_id = u.job_id "
            "LEFT JOIN admission_operations AS a ON a.operation_id = u.job_id "
            "ORDER BY j.created_at_us, u.job_id"
        )
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                rows = connection.execute(query, (limit, worker_id, lease_ms * 1000)).fetchall()
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch claim storage is unavailable") from None
        claims: list[ClaimedJob] = []
        for row in rows:
            claims.append(
                ClaimedJob(
                    _job(row),
                    worker_id,
                    int(row["claim_version"]),
                    int(row["claim_expires_at_us"]),
                )
            )
        return claims


class PostgresDispatchStateStore:
    """Public lifecycle writes and coordination closure share one transaction."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Dispatch transitions require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    async def reload(self, job_id: str) -> DispatchJob:
        return await asyncio.to_thread(self._reload, job_id)

    async def mark_running(self, job: DispatchJob) -> DispatchJob:
        return await asyncio.to_thread(self._mark_running, job)

    async def finish(
        self,
        job: DispatchJob,
        target: str,
        *,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        return await asyncio.to_thread(self._finish, job, target, result_json, error_code)

    def _reload(self, job_id: str) -> DispatchJob:
        identifier(job_id)
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                rows = connection.execute(_JOB_SELECT + "WHERE j.job_id = %s", (job_id,)).fetchall()
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch state is unavailable") from None
        if len(rows) != 1:
            raise DispatchUnavailableError("Dispatch Job identity is absent or duplicated")
        return _job(rows[0])

    def _mark_running(self, job: DispatchJob) -> DispatchJob:
        query = (
            "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 "
            "AS BIGINT) AS now_us), changed AS (UPDATE speech_jobs AS j "
            "SET state = 'running', status_version = j.status_version + 1, "
            "updated_at_us = clock.now_us FROM clock "
            "WHERE j.job_id = %s AND j.status_version = %s AND j.state = 'pending' "
            "RETURNING j.job_id, j.status_version, clock.now_us AS now_us"
            ") INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
            "SELECT changed.job_id, %s, 'running', changed.now_us FROM changed "
            "RETURNING job_id"
        )
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                rows = connection.execute(
                    query, (job.job_id, job.status_version, job.status_version)
                ).fetchall()
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch transition failed") from None
        if rows:
            return replace(job, state="running", status_version=job.status_version + 1)
        current = self._reload(job.job_id)
        if current.state == "running" and current.status_version >= job.status_version + 1:
            return current
        if current.state in ("succeeded", "failed"):
            return current
        raise DispatchConflictError("Public Job did not enter running")

    def _finish(
        self,
        job: DispatchJob,
        target: str,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        if target not in ("succeeded", "failed"):
            raise ValueError("Dispatch can finish only in a terminal state")
        query = (
            "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 "
            "AS BIGINT) AS now_us), changed AS (UPDATE speech_jobs AS j "
            "SET state = %s, status_version = j.status_version + 1, result_json = %s, "
            "error_code = %s, updated_at_us = clock.now_us FROM clock "
            "WHERE j.job_id = %s AND j.status_version = %s AND j.state = %s "
            "RETURNING j.job_id, clock.now_us AS now_us"
            "), event AS (INSERT INTO speech_job_events "
            "(job_id, event_seq, state, occurred_at_us) "
            "SELECT changed.job_id, %s, %s, changed.now_us FROM changed RETURNING job_id"
            "), closed AS (UPDATE coordination_jobs SET job_state = %s, claim_expires_at_us = 0 "
            "WHERE job_id = %s AND job_state = 'pending' RETURNING job_id"
            ") SELECT (SELECT count(*) FROM changed) AS changed, "
            "(SELECT count(*) FROM event) AS evented, "
            "(SELECT count(*) FROM closed) AS closed"
        )
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                row = connection.execute(
                    query,
                    (
                        target,
                        result_json,
                        error_code,
                        job.job_id,
                        job.status_version,
                        job.state,
                        job.status_version,
                        target,
                        target,
                        job.job_id,
                    ),
                ).fetchone()
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch terminal transition failed") from None
        if row is not None and int(row["changed"]) == 1 and int(row["evented"]) == 1:
            return replace(
                job,
                state=cast(Any, target),
                status_version=job.status_version + 1,
            )
        current = self._reload(job.job_id)
        if current.state in ("succeeded", "failed"):
            return current
        raise DispatchConflictError("Public Job terminal transition lost its guard")


__all__ = ["PostgresDispatchQueue", "PostgresDispatchStateStore"]
