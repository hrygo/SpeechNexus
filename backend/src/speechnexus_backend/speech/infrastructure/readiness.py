"""Readiness probes for authoritative and materialized storage."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Sequence
from typing import Any

import httpx
import psycopg
from pydantic import AnyHttpUrl

from speechnexus_backend.speech.infrastructure import (
    job_cancellation_fence_schema,
    public_job_schema,
)


class PostgresReadinessProbe:
    """Verify that the authoritative PostgreSQL endpoint accepts a query."""

    def __init__(self, dsn: str, *, timeout_seconds: float) -> None:
        self._dsn = dsn
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "postgresql"

    async def check(self) -> None:
        connection = await psycopg.AsyncConnection.connect(
            self._dsn,
            connect_timeout=max(1, round(self._timeout_seconds)),
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                row = await cursor.fetchone()
                if row != (1,):
                    raise RuntimeError("PostgreSQL readiness query returned an unexpected result")
        finally:
            await connection.close()


class ObjectStorageReadinessProbe:
    """Verify an explicitly configured object-storage health endpoint."""

    def __init__(self, healthcheck_url: AnyHttpUrl, *, timeout_seconds: float) -> None:
        self._healthcheck_url = healthcheck_url
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "object_storage"

    async def check(self) -> None:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.get(str(self._healthcheck_url))
            response.raise_for_status()


#: Prefix an operator alert matches on. Kept stable and secret-free.
CANCELLATION_DEBT_PREFIX = "cancellation_debt"
#: Identifies the reused readiness session in ``pg_stat_activity``.
_APPLICATION_NAME = "speechnexus-readiness-debt"


class CancellationDebtProbe:
    """Name accepted cancellations that cannot yet return their capacity.

    Read-only, and deliberately non-failing in every failure mode: ``/readyz`` is
    the deployment healthcheck, so one Job holding a slot it cannot return -- or a
    missing privilege, relation, connection or timeout -- must not take the whole
    API out of service. The observed fact is reported in the probe detail instead,
    which reaches both ``/readyz`` and ``GET /v1/nexus/admin/diagnostics``, and the
    alerting rule documented with the operator procedure matches the
    ``cancellation_debt:`` prefix. The probe always reports ``ok``: its detail never
    participates in the aggregate readiness decision.

    ``dsn`` is a dedicated read-only DSN, not the API's own. The API identity is
    not granted the fence, ledger, lease or execution tables -- widening it for a
    diagnostic would trade a security boundary for a string -- so a deployment
    that does not provision such a role passes ``None`` and gets
    ``unavailable (not configured)``. Like ``/readyz`` itself, the probe reuses one
    connection instead of opening a new one on every poll.

    The detail degrades instead of failing, in three observable shapes:

    * ``cancellation_debt: N accepted cancellation(s) ... job -> lease(...)`` --
      the fullest view, available when the runtime role may read the execution
      and lease tables;
    * ``cancellation_debt: N accepted cancellation(s) ... job [lease detail
      unavailable: permission]`` -- the Job names, from the Job, cancellation and
      ledger rows the runtime role owns; the coordination and gateway tables that
      hold the lease and execution states are deliberately *not* granted to it
      (they belong to another owner and hold the canonical command document), so
      the per-lease drill-down stays with the operator SQL documented alongside
      the procedure;
    * ``cancellation_debt: unavailable (<category>)`` -- the probe could not read
      anything; ``<category>`` is one of ``permission``, ``relation``,
      ``connection``, ``timeout`` or ``error`` so a real fault stays visible
      instead of being swallowed.

    ``grace_seconds`` is deliberately larger than the default confirmation window
    plus backoff so an in-flight judgement is not reported as debt.
    """

    def __init__(
        self,
        dsn: str | None,
        *,
        schema: str,
        timeout_seconds: float,
        grace_seconds: float = 900,
        limit: int = 96,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
            raise ValueError("Invalid readiness schema")
        if not 0 < grace_seconds <= 86_400:
            raise ValueError("Cancellation debt grace must be bounded")
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError("Cancellation debt report must be bounded")
        self._dsn = dsn
        self._schema = schema
        self._timeout_seconds = timeout_seconds
        self._grace_us = int(grace_seconds * 1_000_000)
        self._limit = limit
        self._connection: psycopg.AsyncConnection[Any] | None = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "cancellation_debt"

    async def check(self) -> str | None:
        """Report the debt, never the probe's own trouble as unreadiness."""
        if self._dsn is None:
            return f"{CANCELLATION_DEBT_PREFIX}: {_UNAVAILABLE} (not configured)"
        try:
            async with asyncio.timeout(self._timeout_seconds), self._lock:
                try:
                    return await self._inspect()
                except BaseException:
                    # A connection that failed mid-query must not be reused: its
                    # transaction state is unknown.
                    await self._discard()
                    raise
        except Exception as error:
            return unavailable_detail(error)

    async def _inspect(self) -> str | None:
        connection = await self._connection_or_connect()
        async with connection.cursor() as cursor:
            try:
                await cursor.execute(_DEBT, {"grace_us": self._grace_us, "limit": self._limit})
            except _UNREADABLE_TABLES as error:
                # The role is not granted the execution/lease tables (or the schema
                # never dispatched): Job names are still the actionable part, so
                # report them and say which detail is missing.
                await connection.rollback()
                rows = await self._job_level(cursor)
                await connection.rollback()
                return _describe_debt(rows, missing=_failure_category(error))
            rows = await cursor.fetchall()
        # End the read transaction so the reused session stays idle between polls.
        await connection.rollback()
        return _describe_debt(rows)

    async def _connection_or_connect(self) -> psycopg.AsyncConnection[Any]:
        connection = self._connection
        if connection is None or connection.closed or connection.broken:
            assert self._dsn is not None  # guarded by ``check``
            connection = await psycopg.AsyncConnection.connect(
                self._dsn,
                connect_timeout=max(1, round(self._timeout_seconds)),
                options=f"-c search_path={self._schema},pg_catalog,pg_temp",
                application_name=_APPLICATION_NAME,
            )
            self._connection = connection
        return connection

    async def _job_level(self, cursor: psycopg.AsyncCursor[Any]) -> list[tuple[Any, ...]]:
        await cursor.execute(_JOB_LEVEL_DEBT, {"grace_us": self._grace_us, "limit": self._limit})
        return list(await cursor.fetchall())

    async def _discard(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        with contextlib.suppress(Exception):  # a close failure has no caller to inform
            await connection.close()

    async def aclose(self) -> None:
        """Release the reused session; callers own the process lifetime."""
        async with self._lock:
            await self._discard()


#: Stable, secret-free detail punctuation for a probe that could not read everything.
_UNAVAILABLE = "unavailable"
#: The role may not read the execution/lease tables, or the schema never had them.
_UNREADABLE_TABLES = (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedTable)


def unavailable_detail(error: BaseException) -> str:
    """One bounded sentence naming what the probe could not read, and why."""
    return f"{CANCELLATION_DEBT_PREFIX}: {_UNAVAILABLE} ({_failure_category(error)})"


def _failure_category(error: BaseException) -> str:
    """Classify a probe failure so a real fault stays visible in the detail."""
    if isinstance(error, psycopg.errors.InsufficientPrivilege):
        return "permission"
    if isinstance(error, psycopg.errors.UndefinedTable):
        return "relation"
    if isinstance(error, TimeoutError | asyncio.TimeoutError):
        return "timeout"
    if isinstance(error, psycopg.OperationalError | psycopg.InterfaceError | OSError):
        return "connection"
    return "error"


def _describe_debt(rows: Sequence[tuple[Any, ...]], *, missing: str | None = None) -> str | None:
    """One bounded sentence naming Job, lease and both states per blocked row."""
    if not rows:
        return None
    jobs: dict[str, list[str]] = {}
    for job_id, lease_id, lease_state, execution_state in (
        (row[0], row[1], row[2], row[3]) for row in rows
    ):
        if lease_id is None:
            # Job-level rows carry no lease identity: say so instead of inventing one.
            label = "no-held-lease" if missing is None else "lease-unread"
            jobs.setdefault(job_id, []).append(label)
            continue
        jobs.setdefault(job_id, []).append(
            f"{lease_id}(lease={lease_state}, execution={execution_state or 'not_submitted'})"
        )
    shown = list(jobs.items())[:3]
    detail = "; ".join(f"{job} -> {', '.join(leases)}" for job, leases in shown)
    hidden = len(jobs) - len(shown)
    suffix = f" (+{hidden} more)" if hidden else ""
    note = f" [lease detail {_UNAVAILABLE}: {missing}]" if missing else ""
    return (
        f"{CANCELLATION_DEBT_PREFIX}: {len(jobs)} accepted cancellation(s) still hold "
        f"capacity: {detail}{suffix}{note}"
    )


_JOB_LEVEL_DEBT = f"""
SELECT j.job_id, NULL, NULL, NULL
FROM {job_cancellation_fence_schema.TABLE} AS f
JOIN {public_job_schema.JOBS} AS j ON j.job_id = f.job_id
LEFT JOIN {job_cancellation_fence_schema.RECONCILIATION_TABLE} AS r ON r.job_id = f.job_id
WHERE j.state = 'cancelling'
  AND (r.job_id IS NULL OR r.settled_at_us IS NULL)
  AND f.accepted_at_us
      <= CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) - %(grace_us)s
ORDER BY f.accepted_at_us, f.job_id
LIMIT %(limit)s
"""

_DEBT = f"""
SELECT j.job_id, l.lease_id, l.lease_state, i.execution_state
FROM {job_cancellation_fence_schema.TABLE} AS f
JOIN {public_job_schema.JOBS} AS j ON j.job_id = f.job_id
LEFT JOIN {job_cancellation_fence_schema.RECONCILIATION_TABLE} AS r ON r.job_id = f.job_id
-- Only a lease that still holds capacity is a blocker: `quarantined` is an
-- accounted state and `released` returned the capacity.
LEFT JOIN admission_leases AS l
       ON l.operation_id = f.job_id
      AND l.lease_state NOT IN ('released', 'quarantined')
LEFT JOIN submit_intents AS i ON i.lease_id = l.lease_id
WHERE j.state = 'cancelling'
  -- A fence with no ledger row is a deployment that never ran the worker: the
  -- acceptance is unclaimed debt, not a settled one.
  AND (r.job_id IS NULL OR r.settled_at_us IS NULL)
  AND f.accepted_at_us
      <= CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) - %(grace_us)s
ORDER BY f.accepted_at_us, f.job_id, l.lease_id
LIMIT %(limit)s
"""
