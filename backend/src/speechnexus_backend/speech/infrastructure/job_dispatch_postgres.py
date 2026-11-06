"""PostgreSQL claim lease and public Job transitions for the dispatcher."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.job_dispatch import (
    ClaimedJob,
    DispatchClaimLostError,
    DispatchConflictError,
    DispatchJob,
    DispatchProtocolError,
    DispatchUnavailableError,
    identifier,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    NOT_SUBMITTED_TABLE,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_JOB_SELECT = (
    "SELECT j.job_id, j.workspace_id, j.kind, j.state, j.status_version, "
    "j.created_at_us, j.request_digest, COALESCE(a.request_document, '') AS request_document "
    "FROM speech_jobs AS j "
    "LEFT JOIN admission_operations AS a ON a.operation_id = j.job_id "
)


class _TerminalGuardLostError(RuntimeError):
    """The current transaction must roll back without writing a terminal state."""


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

    async def renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob | None:
        identifier(claim.worker_id)
        if not 1_000 <= lease_ms <= 60_000:
            raise ValueError("Dispatch renewal bounds are invalid")
        return await asyncio.to_thread(self._renew, claim, lease_ms)

    async def record_cancellation_not_submitted(self, job_id: str, *, observer: str) -> None:
        identifier(job_id)
        identifier(observer)
        return await asyncio.to_thread(self._record_cancellation_not_submitted, job_id, observer)

    async def record_progress(
        self, claim: ClaimedJob, *, unit: str, completed: int, total: int | None
    ) -> bool:
        if unit not in ("audio_ms", "text_codepoints", "items"):
            raise ValueError("Progress unit is invalid")
        if type(completed) is not int or completed < 0:
            raise ValueError("Progress value is invalid")
        if total is not None and (type(total) is not int or total < completed):
            raise ValueError("Progress total is invalid")
        return await asyncio.to_thread(self._record_progress, claim, unit, completed, total)

    async def finish(
        self,
        claim: ClaimedJob,
        target: str,
        *,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        return await asyncio.to_thread(self._finish, claim, target, result_json, error_code)

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

    def _renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob | None:
        query = (
            "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 "
            "AS BIGINT) AS now_us) "
            "UPDATE coordination_jobs AS c SET claim_expires_at_us = clock.now_us + %s "
            "FROM clock WHERE c.job_id = %s AND c.claim_owner_id = %s "
            "AND c.claim_version = %s AND c.job_state = 'pending' "
            "RETURNING c.claim_expires_at_us"
        )
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                rows = connection.execute(
                    query,
                    (lease_ms * 1000, claim.job.job_id, claim.worker_id, claim.claim_version),
                ).fetchall()
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch lease renewal is unavailable") from None
        if not rows:
            return None
        return replace(claim, lease_expires_at_us=int(rows[0]["claim_expires_at_us"]))

    def _record_progress(
        self, claim: ClaimedJob, unit: str, completed: int, total: int | None
    ) -> bool:
        """Append one strictly advancing progress fact while this claim owns the Job.

        The row is committed only for a running public Job whose declared unit
        matches the fact. The job row lock serialises this append with terminal
        transitions; the event guard rejects a terminal Job, a non-advancing
        value or an out-of-order sequence even if a writer bypasses this check.
        """
        job = claim.job
        query = (
            "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) "
            "* 1000000 AS BIGINT) AS now_us) "
            "SELECT c.job_id FROM coordination_jobs AS c CROSS JOIN clock "
            "WHERE c.job_id = %s AND c.claim_owner_id = %s AND c.claim_version = %s "
            "AND c.job_state = 'pending' AND c.claim_expires_at_us > clock.now_us "
            "FOR UPDATE OF c"
        )
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                owned = connection.execute(
                    query, (job.job_id, claim.worker_id, claim.claim_version)
                ).fetchone()
                if owned is None:
                    return False
                current = connection.execute(
                    "SELECT j.state, s.progress_unit FROM speech_jobs AS j "
                    "JOIN speech_job_submissions AS s ON s.job_id = j.job_id "
                    "WHERE j.job_id = %s FOR UPDATE OF j",
                    (job.job_id,),
                ).fetchone()
                if (
                    current is None
                    or current["state"] != "running"
                    or current["progress_unit"] != unit
                ):
                    return False
                latest = connection.execute(
                    "SELECT COALESCE(MAX(event_seq), -1) AS seq, "
                    "COALESCE(MAX(progress_completed) FILTER "
                    "(WHERE kind = 'progress'), -1) AS completed "
                    "FROM speech_job_events WHERE job_id = %s",
                    (job.job_id,),
                ).fetchone()
                if latest is None or completed <= int(latest["completed"]):
                    return False
                connection.execute(
                    "INSERT INTO speech_job_events "
                    "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
                    "progress_completed, progress_total) VALUES (%s, %s, 'running', "
                    "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT), "
                    "'progress', %s, %s, %s)",
                    (job.job_id, int(latest["seq"]) + 1, unit, completed, total),
                )
            return True
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch progress storage failed") from None

    def _record_cancellation_not_submitted(self, job_id: str, observer: str) -> None:
        query = (
            "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 "
            "AS BIGINT) AS now_us) "
            f"INSERT INTO {NOT_SUBMITTED_TABLE} (job_id, observed_at_us, observer) "
            "SELECT j.job_id, clock.now_us, %s FROM speech_jobs AS j CROSS JOIN clock "
            "WHERE j.job_id = %s AND j.state = 'cancelling' "
            "ON CONFLICT (job_id) DO NOTHING"
        )
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                connection.execute(query, (observer, job_id))
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError(
                "Cancellation not-submitted observation is unavailable"
            ) from None

    def _finish(
        self,
        claim: ClaimedJob,
        target: str,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        if target not in ("succeeded", "failed"):
            raise ValueError("Dispatch can finish only in a terminal state")
        job = claim.job
        terminal: DispatchJob | None = None
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                owned = connection.execute(
                    "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) "
                    "* 1000000 AS BIGINT) AS now_us) "
                    "SELECT c.job_id FROM coordination_jobs AS c CROSS JOIN clock "
                    "WHERE c.job_id = %s AND c.claim_owner_id = %s "
                    "AND c.claim_version = %s AND c.job_state = 'pending' "
                    "AND c.claim_expires_at_us > clock.now_us FOR UPDATE OF c",
                    (job.job_id, claim.worker_id, claim.claim_version),
                ).fetchone()
                if owned is None:
                    # No public or event write may occur without the current lease.
                    raise _TerminalGuardLostError
                changed = connection.execute(
                    "WITH clock AS (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) "
                    "* 1000000 AS BIGINT) AS now_us) "
                    "UPDATE speech_jobs AS j SET state = %s, "
                    "status_version = j.status_version + 1, result_json = %s, "
                    "error_code = %s, updated_at_us = clock.now_us FROM clock "
                    "WHERE j.job_id = %s AND j.status_version = %s AND j.state = %s "
                    "RETURNING clock.now_us AS now_us",
                    (
                        target,
                        result_json,
                        error_code,
                        job.job_id,
                        job.status_version,
                        job.state,
                    ),
                ).fetchone()
                if changed is None:
                    # The public Job was settled or advanced by another writer.
                    raise _TerminalGuardLostError
                evented = connection.execute(
                    "INSERT INTO speech_job_events "
                    "(job_id, event_seq, state, occurred_at_us) "
                    "SELECT %s, COALESCE(MAX(event_seq), -1) + 1, %s, %s "
                    "FROM speech_job_events WHERE job_id = %s RETURNING job_id",
                    (job.job_id, target, int(changed["now_us"]), job.job_id),
                ).fetchone()
                if evented is None:
                    raise _TerminalGuardLostError
                closed = connection.execute(
                    "UPDATE coordination_jobs AS c SET job_state = %s, claim_expires_at_us = 0 "
                    "WHERE c.job_id = %s AND c.job_state = 'pending' "
                    "AND c.claim_owner_id = %s AND c.claim_version = %s "
                    "AND c.claim_expires_at_us > CAST(EXTRACT(EPOCH FROM clock_timestamp()) "
                    "* 1000000 AS BIGINT) RETURNING c.job_id",
                    (target, job.job_id, claim.worker_id, claim.claim_version),
                ).fetchone()
                if closed is None:
                    # Roll back the public transition if the lease expired before close.
                    raise _TerminalGuardLostError
                terminal = replace(
                    job,
                    state=cast(Any, target),
                    status_version=job.status_version + 1,
                )
        except _TerminalGuardLostError:
            terminal = None
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise DispatchUnavailableError("Dispatch terminal transition failed") from None
        if terminal is not None:
            return terminal
        raise DispatchClaimLostError("Dispatch lease was lost before the terminal transition")


__all__ = ["PostgresDispatchQueue", "PostgresDispatchStateStore"]
