"""Readiness probes for authoritative and materialized storage."""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
from collections.abc import Sequence
from typing import Any

import httpx
import psycopg
from pydantic import AnyHttpUrl

from speechnexus_backend.speech.domain.job_dispatch import VERIFIED_STOP_EVIDENCE_KINDS
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


#: Fraction of the service's per-probe budget a non-failing diagnostic spends on its own
#: work. ``ReadinessService`` wraps every probe in ``asyncio.timeout(timeout_seconds)``, so a
#: probe whose own budget equals the service budget races it: the outer timeout wins, the
#: probe's ``except Exception`` never runs, and ``/readyz`` reports ``failed``/``timed out``.
#: That turns a diagnostic into an outage -- exactly what these probes promise never to do.
#: Spending 80% leaves the probe room to convert its own timeout into the
#: ``unavailable (timeout)`` detail it documents.
_PROBE_BUDGET_FRACTION = 0.8
#: Hard character bound on a debt detail. Row and job counts are already bounded, but 96 rows
#: of long identifiers still produce a ~17 kB string, and a readiness detail that large is a
#: payload problem rather than a diagnostic. ASCII identifiers make characters equal bytes.
_DETAIL_MAX_CHARS = 512
#: Appended when the detail is cut, so a truncated sentence never looks complete.
_DETAIL_TRUNCATED = "... (truncated)"

#: Prefix an operator alert matches on. Kept stable and secret-free.
CANCELLATION_DEBT_PREFIX = "cancellation_debt"
#: The subclass of an accepted cancellation whose execution already succeeded while
#: its lease stayed reserved. It has its own bounded operator exit
#: (``python -m speechnexus_backend.completed_release_cli``); it is *not* the
#: unknown-execution debt and must never be handled as one.
COMPLETED_WITHOUT_RELEASE = "completed-without-release"
#: Every other blocking lease: the execution is unknown, still running, never
#: submitted, or succeeded without the gateway's terminal fact. Those reach a
#: determinate state through isolation, a verified stop, or #338.
EXECUTION_UNKNOWN = "execution-unknown"
#: Fixed report order for held leases: the actionable subclass first, then the rest.
_LEASE_CLASSES = (COMPLETED_WITHOUT_RELEASE, EXECUTION_UNKNOWN)
#: The Job-level labels, used when the lease itself cannot be read.
_JOB_CLASSES = ("lease-unread", "no-held-lease")
#: Fixed report order and wording for the ledger-side schedule facts. The three are
#: mutually exclusive on one acceptance, and each states a fact about the durable
#: ledger as of this read -- never about a process: the probe cannot see whether a
#: worker is running or whether reconciliation is enabled. Naming them is what stops
#: "still holds capacity" from reading as progress (#852).
_SCHEDULE_SCOPES: tuple[tuple[str, str], ...] = (
    ("never_claimed", "never claimed"),
    ("exhausted", "never claimable (exhausted)"),
    ("due", "due now"),
)
#: The database clock, read once per query: the grace filter and the ``due`` state
#: must agree on "now", and neither may use the process clock.
_NOW_CLOCK = (
    "CROSS JOIN (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) "
    "AS now_us) AS now_clock"
)
#: One computed schedule state per acceptance, shared verbatim by both debt reads so
#: the full and the Job-level view cannot classify the same row differently. The
#: branches mirror the reconciliation store's ``_CLAIM`` predicate: no ledger row means
#: never claimed, ``exhausted`` is never claimed again, and ``next_check_at_us`` is the
#: schedule that makes a row claimable now.
_SCHEDULE_STATE_SQL = """CASE
           WHEN r.job_id IS NULL THEN 'never_claimed'
           WHEN r.settled_at_us IS NOT NULL THEN 'settled'
           WHEN r.last_outcome = 'exhausted' THEN 'exhausted'
           WHEN r.next_check_at_us <= now_clock.now_us THEN 'due'
           ELSE 'scheduled'
       END"""
#: The one evidence kind that makes a completed execution releasable by testimony.
_TERMINAL_EVIDENCE = "engine_terminal_response"
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

    The parenthetical also splits the same Job count by the durable ledger fact that
    decides *what can advance each acceptance*, because "still holds capacity" alone
    reads as an in-progress cancellation -- the misinformation #852 recorded. The
    three marks are mutually exclusive, and each proves only a ledger fact, never
    that a process is running: the probe cannot see workers, and it cannot see the
    reconciliation switch.

    * ``never claimed: N`` -- the fence has no ledger row, so nothing has claimed this
      acceptance since the fence was written. A first claim removes the mark, so its
      absence never means a worker is running;
    * ``never claimable (exhausted): N`` -- the ledger row is ``exhausted``, which the
      worker's claim predicate never selects again: only an operator exit can close
      it, and running the worker for another pass is not one;
    * ``due now: N`` -- the ledger row is claimable at this instant
      (``next_check_at_us <= now``, not settled, not exhausted). A healthy worker also
      lands here between polls, so only a mark that persists says nobody is advancing.

    The three counts are part of the counted scopes, ahead of the per-Job detail, so
    detail truncation cannot drop them; each counts Jobs, not leases.

    The detail degrades instead of failing, in three observable shapes:

    * ``cancellation_debt: N accepted cancellation(s) ... (completed-without-release=1,
      execution-unknown=1): job -> lease(... subclass=...)`` -- the fullest view,
      available when the runtime role may read the execution and lease tables. The
      parenthesised counts and the per-lease ``subclass`` always name which of the
      two dispositions the row needs; ``completed-without-release`` is the shape the
      operator exit closes, every other class stays with isolation and the verified
      stop path;
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
        # Spend only part of the service budget, and give libpq a longer connect timeout than
        # that: the probe's own deadline must be the one that fires, so a hung endpoint becomes
        # this probe's ``unavailable (timeout)`` detail instead of the service's ``failed``.
        self._timeout_seconds = timeout_seconds * _PROBE_BUDGET_FRACTION
        self._connect_timeout_seconds = max(1, math.ceil(timeout_seconds) + 1)
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
                connect_timeout=self._connect_timeout_seconds,
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


def _bounded_detail(text: str) -> str:
    """Cut a debt detail at a separator so the result stays a readable, bounded sentence."""
    if len(text) <= _DETAIL_MAX_CHARS:
        return text
    budget = _DETAIL_MAX_CHARS - len(_DETAIL_TRUNCATED)
    window = text[:budget]
    # Prefer a job/lease boundary, then a token boundary: a mid-identifier cut reads as a
    # different identifier, which is worse than a shorter list.
    for separator in ("; ", ", ", " "):
        cut = window.rfind(separator)
        if cut > budget // 2:
            return window[:cut].rstrip() + _DETAIL_TRUNCATED
    return window.rstrip() + _DETAIL_TRUNCATED


def debt_subclass(
    lease_state: str | None,
    execution_state: str | None,
    *,
    evidence_kind: str | None = None,
    execution_stopped: int | None = None,
    resources_released: int | None = None,
) -> str:
    """Name which accepted cancellation this lease is, so one procedure never covers both.

    ``completed-without-release`` is the shape the operator exit accepts, so the
    label applies only when that exit's own gate would: a ``reserved`` lease whose
    execution is ``succeeded`` *and* whose gateway terminal fact is recorded with
    both flags. A ``succeeded`` execution without that fact is #338's shape and
    stays ``execution-unknown`` -- labelling it with an exit that refuses it is
    worse than no label, because the label's only use is routing.
    """
    completed = (
        lease_state == "reserved"
        and execution_state == "succeeded"
        and evidence_kind == _TERMINAL_EVIDENCE
        and execution_stopped == 1
        and resources_released == 1
    )
    return COMPLETED_WITHOUT_RELEASE if completed else EXECUTION_UNKNOWN


def _describe_debt(rows: Sequence[tuple[Any, ...]], *, missing: str | None = None) -> str | None:
    """One bounded sentence: two named units, the schedule split, then the drill-down.

    The sentence counts accepted cancellations (Jobs); the parenthetical counts the
    leases or the unreadable Jobs it is actually classifying, plus the three ledger
    facts that say what can still advance each Job. Naming the units is what stops a
    multi-stage Job from making "3 cancellations" sit next to four subclass counts,
    and what stops "still holds capacity" from reading as progress (#852).
    """
    if not rows:
        return None
    jobs: dict[str, list[str]] = {}
    lease_counts: dict[str, int] = {}
    job_counts: dict[str, int] = {}
    schedule_jobs: dict[str, set[str]] = {state: set() for state, _ in _SCHEDULE_SCOPES}
    for row in rows:
        job_id, lease_id, lease_state, execution_state = row[0], row[1], row[2], row[3]
        # The state decides what can advance this acceptance, so it is counted per Job:
        # every lease row of one Job repeats the same ledger fact. The guard keeps
        # synthetic rows (and a query that does not carry the column) from inventing one.
        schedule = row[7] if len(row) > 7 else None
        if isinstance(schedule, str) and schedule in schedule_jobs:
            schedule_jobs[schedule].add(job_id)
        if lease_id is None:
            # Job-level rows carry no lease identity: say so instead of inventing one.
            label = "no-held-lease" if missing is None else "lease-unread"
            jobs.setdefault(job_id, []).append(label)
            job_counts[label] = job_counts.get(label, 0) + 1
            continue
        subclass = debt_subclass(
            lease_state,
            execution_state,
            evidence_kind=row[4] if len(row) > 4 else None,
            execution_stopped=row[5] if len(row) > 5 else None,
            resources_released=row[6] if len(row) > 6 else None,
        )
        lease_counts[subclass] = lease_counts.get(subclass, 0) + 1
        jobs.setdefault(job_id, []).append(
            f"{lease_id}(lease={lease_state}, "
            f"execution={execution_state or 'not_submitted'}, subclass={subclass})"
        )
    shown = list(jobs.items())[:3]
    detail = "; ".join(f"{job} -> {', '.join(leases)}" for job, leases in shown)
    hidden = len(jobs) - len(shown)
    suffix = f" (+{hidden} more)" if hidden else ""
    note = f" [lease detail {_UNAVAILABLE}: {missing}]" if missing else ""
    leases = sum(lease_counts.values())
    head = f"{CANCELLATION_DEBT_PREFIX}: {len(jobs)} accepted cancellation(s) still hold capacity"
    head += f" over {leases} held lease(s)" if leases else ""
    scopes = []
    if lease_counts:
        scopes.append(
            "held leases: "
            + ", ".join(
                f"{name}={lease_counts[name]}" for name in _LEASE_CLASSES if lease_counts.get(name)
            )
        )
    if job_counts:
        scopes.append(
            "jobs: "
            + ", ".join(
                f"{name}={job_counts[name]}" for name in _JOB_CLASSES if job_counts.get(name)
            )
        )
    for state, name in _SCHEDULE_SCOPES:
        # Fixed order and one entry per non-empty state: the operator reads this as the
        # split of the same Job count, so it may not depend on which rows came back first.
        if schedule_jobs[state]:
            scopes.append(f"{name}: {len(schedule_jobs[state])}")
    return _bounded_detail(f"{head} ({'; '.join(scopes)}): {detail}{suffix}{note}")


_JOB_LEVEL_DEBT = f"""
SELECT j.job_id, NULL, NULL, NULL, NULL, NULL, NULL,
       {_SCHEDULE_STATE_SQL} AS schedule_state
FROM {job_cancellation_fence_schema.TABLE} AS f
JOIN {public_job_schema.JOBS} AS j ON j.job_id = f.job_id
LEFT JOIN {job_cancellation_fence_schema.RECONCILIATION_TABLE} AS r ON r.job_id = f.job_id
{_NOW_CLOCK}
WHERE j.state = 'cancelling'
  AND (r.job_id IS NULL OR r.settled_at_us IS NULL)
  AND f.accepted_at_us <= now_clock.now_us - %(grace_us)s
ORDER BY f.accepted_at_us, f.job_id
LIMIT %(limit)s
"""

# Both debt reads below are polled every 5 s by the deployment healthcheck, so their cost
# must follow the debt rather than the deployment's history. `speech_jobs_live_state_scan`
# (migration 0056) is what keeps them off the Job table and off the perpetual cancellation
# fence history: without it the planner has no access path for `state = 'cancelling'` here
# and walks every fence the deployment ever wrote, which is a cost that grows with uptime.
# The index only holds `running`/`cancelling` rows, so the write cost stays with the live set.
_DEBT = f"""
SELECT j.job_id, l.lease_id, l.lease_state, i.execution_state,
       e.evidence_kind, e.execution_stopped, e.resources_released,
       -- One ledger-side schedule fact per acceptance: never claimed, permanently
       -- unclaimable, or claimable now. The read face names all three so "still holds
       -- capacity" is never the only thing an operator sees (#852).
       {_SCHEDULE_STATE_SQL} AS schedule_state
FROM {job_cancellation_fence_schema.TABLE} AS f
JOIN {public_job_schema.JOBS} AS j ON j.job_id = f.job_id
LEFT JOIN {job_cancellation_fence_schema.RECONCILIATION_TABLE} AS r ON r.job_id = f.job_id
{_NOW_CLOCK}
-- Only a lease that still holds capacity is a blocker: `quarantined` is an
-- accounted state and `released` returned the capacity.
LEFT JOIN admission_leases AS l
       ON l.operation_id = f.job_id
      AND l.lease_state NOT IN ('released', 'quarantined')
LEFT JOIN submit_intents AS i ON i.lease_id = l.lease_id
-- The subclass label must match the exit's own gate: a completed execution with
-- no terminal fact is #338's shape, and routing it to that exit would send the
-- operator to a command that always refuses it.
LEFT JOIN execution_stop_evidence AS e ON e.submit_intent_id = i.submit_intent_id
WHERE j.state = 'cancelling'
  -- A fence with no ledger row is unclaimed debt; the schedule state above names it.
  AND (r.job_id IS NULL OR r.settled_at_us IS NULL)
  AND f.accepted_at_us <= now_clock.now_us - %(grace_us)s
ORDER BY f.accepted_at_us, f.job_id, l.lease_id
LIMIT %(limit)s
"""


#: Prefix an operator alert matches on for the dispatch gate's stuck Jobs. Sibling of
#: ``CANCELLATION_DEBT_PREFIX`` and deliberately distinct: this class has no automatic
#: exit, and its remedy is the producer, not a settlement command.
STOP_EVIDENCE_DEBT_PREFIX = "stop_evidence_debt"
#: The stop-evidence kinds the coordination contract accepts as a verified stop, as one
#: SQL tuple. It is derived from the domain's single definition: a second literal here
#: would drift, and drift in this direction is silent -- the probe would stop matching the
#: kind the dispatch gate accepts and report no debt for the Jobs that need it.
_SQL_VERIFIED_STOP_EVIDENCE = (
    "(" + ", ".join(f"'{kind}'" for kind in sorted(VERIFIED_STOP_EVIDENCE_KINDS)) + ")"
)

# Same access-path requirement as `_DEBT`: `speech_jobs_live_state_scan` (migration 0056)
# is what keeps this read off the whole Job table once the held-lease backlog is large
# enough that a table scan beats the lease-driven nested loop -- measured at 37.8 ms with
# 20 000 held leases over 1 000 000 Jobs, against 0.6 ms with 64.
_STOP_EVIDENCE_DEBT = f"""
SELECT j.job_id, i.submit_intent_id, i.execution_state, i.cleanup_state, e.evidence_kind
FROM {public_job_schema.JOBS} AS j
-- Only a lease that still holds capacity is a blocker: `quarantined` is an accounted
-- state and `released` returned the capacity.
JOIN admission_leases AS l
  ON l.operation_id = j.job_id
 AND l.lease_state NOT IN ('released', 'quarantined')
JOIN submit_intents AS i ON i.lease_id = l.lease_id
LEFT JOIN execution_stop_evidence AS e ON e.submit_intent_id = i.submit_intent_id
WHERE j.state = 'running'
  AND i.execution_state IN ('failed', 'aborted')
  -- The provable non-dispatch shape is not this debt: the dispatcher settles it.
  AND NOT (
        i.cleanup_state = 'not_required'
    AND i.dispatched_at_us IS NULL
    AND i.engine_request_id IS NULL
  )
  -- Neither is a verified stop: a lease that outlived its own verified release is the
  -- `cancellation_debt` completed-without-release shape, not a missing evidence fact.
  -- COALESCE is load-bearing: a missing evidence row leaves every joined column NULL,
  -- and `NOT NULL` is NULL, which would silently drop the one class this probe exists
  -- to report. "No row" means "no verified stop", i.e. TRUE here.
  AND NOT COALESCE(
        e.evidence_kind IN {_SQL_VERIFIED_STOP_EVIDENCE}
    AND e.execution_stopped = 1
    AND e.resources_released = 1,
    FALSE
  )
  AND j.updated_at_us
      <= CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) - %(grace_us)s
ORDER BY j.updated_at_us, j.job_id
LIMIT %(limit)s
"""


def stop_evidence_unavailable_detail(error: BaseException) -> str:
    """One bounded sentence naming what this probe could not read, and why."""
    return f"{STOP_EVIDENCE_DEBT_PREFIX}: {_UNAVAILABLE} ({_failure_category(error)})"


def _describe_stop_evidence_debt(
    rows: Sequence[tuple[Any, ...]], *, missing: str | None = None
) -> str | None:
    """One bounded sentence naming every `running` Job the dispatch gate refused to settle.

    The gate holds a Job whose gateway reported ``failed``/``aborted`` without a verified
    stop, because projecting the failure would return capacity for a slot nothing proved
    released. That refusal is correct and has no automatic exit, so it must be visible:
    this detail is the operator's list, the fixed prefix is the alerting key, and the
    remedy is always the producer's status document.
    """
    if not rows:
        return None
    jobs: dict[str, list[str]] = {}
    for row in rows:
        job_id, intent_id, execution_state, cleanup_state = row[0], row[1], row[2], row[3]
        kind = row[4] if len(row) > 4 else None
        jobs.setdefault(job_id, []).append(
            f"{intent_id}(execution={execution_state or 'not_submitted'}, "
            f"cleanup={cleanup_state or 'unrecorded'}, "
            f"evidence={kind or 'none'})"
        )
    shown = list(jobs.items())[:3]
    detail = "; ".join(f"{job} -> {', '.join(intents)}" for job, intents in shown)
    hidden = len(jobs) - len(shown)
    suffix = f" (+{hidden} more)" if hidden else ""
    note = f" [lease detail {_UNAVAILABLE}: {missing}]" if missing else ""
    executions = sum(len(intents) for intents in jobs.values())
    head = (
        f"{STOP_EVIDENCE_DEBT_PREFIX}: {len(jobs)} running Job(s) hold capacity while the "
        f"gateway reports no verified stop"
    )
    head += f" over {executions} execution(s)" if executions else ""
    return _bounded_detail(f"{head}: {detail}{suffix}{note}")


class StopEvidenceDebtProbe:
    """Name `running` Jobs whose failure cannot be projected for want of stop evidence.

    Read-only and non-failing for the same reason as :class:`CancellationDebtProbe`:
    ``/readyz`` is the deployment healthcheck, so one Job the dispatch gate had to hold
    -- or a missing privilege, relation, connection or timeout -- must not take the API
    out of service. The observed fact is reported in the probe detail instead, which
    reaches both ``/readyz`` and ``GET /v1/nexus/admin/diagnostics``, and the alerting
    rule matches the ``stop_evidence_debt:`` prefix.

    It shares the optional read-only DSN with the cancellation debt probe: the tables it
    reads are the same ones, and the API identity is not granted them. A deployment
    without such a role gets ``stop_evidence_debt: unavailable (not configured)``.

    The disposition is deliberately *not* a settlement: nothing here decides the Job.
    The operator fixes the producer -- the gateway, or a test double, must report
    ``cleanup_state`` and ``stop_evidence_kind`` as the frozen contract requires -- and
    the next dispatch pass then settles the Job through the normal path.
    """

    def __init__(
        self,
        dsn: str | None,
        *,
        schema: str,
        timeout_seconds: float,
        grace_seconds: float = 300,
        limit: int = 96,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
            raise ValueError("Invalid readiness schema")
        if not 0 < grace_seconds <= 86_400:
            raise ValueError("Stop evidence debt grace must be bounded")
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError("Stop evidence debt report must be bounded")
        self._dsn = dsn
        self._schema = schema
        # Spend only part of the service budget, and give libpq a longer connect timeout than
        # that: the probe's own deadline must be the one that fires, so a hung endpoint becomes
        # this probe's ``unavailable (timeout)`` detail instead of the service's ``failed``.
        self._timeout_seconds = timeout_seconds * _PROBE_BUDGET_FRACTION
        self._connect_timeout_seconds = max(1, math.ceil(timeout_seconds) + 1)
        self._grace_us = int(grace_seconds * 1_000_000)
        self._limit = limit
        self._connection: psycopg.AsyncConnection[Any] | None = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return STOP_EVIDENCE_DEBT_PREFIX

    async def check(self) -> str | None:
        """Report the debt, never the probe's own trouble as unreadiness."""
        if self._dsn is None:
            return f"{STOP_EVIDENCE_DEBT_PREFIX}: {_UNAVAILABLE} (not configured)"
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
            return stop_evidence_unavailable_detail(error)

    async def _inspect(self) -> str | None:
        connection = await self._connection_or_connect()
        async with connection.cursor() as cursor:
            await cursor.execute(
                _STOP_EVIDENCE_DEBT, {"grace_us": self._grace_us, "limit": self._limit}
            )
            rows = await cursor.fetchall()
        # End the read transaction so the reused session stays idle between polls.
        await connection.rollback()
        return _describe_stop_evidence_debt(rows)

    async def _connection_or_connect(self) -> psycopg.AsyncConnection[Any]:
        connection = self._connection
        if connection is None or connection.closed or connection.broken:
            assert self._dsn is not None  # guarded by ``check``
            connection = await psycopg.AsyncConnection.connect(
                self._dsn,
                connect_timeout=self._connect_timeout_seconds,
                options=f"-c search_path={self._schema},pg_catalog,pg_temp",
                application_name=_APPLICATION_NAME,
            )
            self._connection = connection
        return connection

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
