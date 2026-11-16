"""Readiness probes for authoritative and materialized storage."""

from __future__ import annotations

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


class CancellationDebtProbe:
    """Name accepted cancellations that cannot yet return their capacity.

    Read-only, and deliberately non-failing: ``/readyz`` is the deployment
    healthcheck, so one Job holding a slot it cannot return must not take the
    whole API out of service. The fact is reported in the probe detail instead,
    which reaches both ``/readyz`` and ``GET /v1/nexus/admin/diagnostics``, and
    the alerting rule documented with the operator procedure matches the
    ``cancellation_debt:`` prefix.

    ``grace_seconds`` is deliberately larger than the default confirmation window
    plus backoff so an in-flight judgement is not reported as debt.
    """

    def __init__(
        self,
        dsn: str,
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

    @property
    def name(self) -> str:
        return "cancellation_debt"

    async def check(self) -> str | None:
        connection = await psycopg.AsyncConnection.connect(
            self._dsn,
            connect_timeout=max(1, round(self._timeout_seconds)),
            options=f"-c search_path={self._schema},pg_catalog,pg_temp",
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(_DEBT, {"grace_us": self._grace_us, "limit": self._limit})
                rows = await cursor.fetchall()
        finally:
            await connection.close()
        return _describe_debt(rows)


def _describe_debt(rows: Sequence[tuple[Any, ...]]) -> str | None:
    """One bounded sentence naming Job, lease and both states per blocked row."""
    if not rows:
        return None
    jobs: dict[str, list[str]] = {}
    for job_id, lease_id, lease_state, execution_state in (
        (row[0], row[1], row[2], row[3]) for row in rows
    ):
        if lease_id is None:
            jobs.setdefault(job_id, []).append("no-held-lease")
            continue
        jobs.setdefault(job_id, []).append(
            f"{lease_id}(lease={lease_state}, execution={execution_state or 'not_submitted'})"
        )
    shown = list(jobs.items())[:3]
    detail = "; ".join(f"{job} -> {', '.join(leases)}" for job, leases in shown)
    hidden = len(jobs) - len(shown)
    suffix = f" (+{hidden} more)" if hidden else ""
    return (
        f"{CANCELLATION_DEBT_PREFIX}: {len(jobs)} accepted cancellation(s) still hold "
        f"capacity: {detail}{suffix}"
    )


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
