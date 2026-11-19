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
#: The Job-state side fact reported in addition to the three ledger marks: the
#: cancellation is still unsettled and the Job has *left* ``cancelling`` while one of
#: its leases still holds capacity. It is a fact about the Job row, not about the
#: ledger's schedule, so it is a fourth scope and never joins the three marks above --
#: a Job may carry both a ledger mark and this label (#852 residual, read-face blind
#: spot).
LEFT_CANCELLING_HOLDING_CAPACITY = "left-cancelling-holding-capacity"
#: The unbounded sibling of :data:`LEFT_CANCELLING_HOLDING_CAPACITY`: the same
#: accepted-cancellation-and-held-lease fact, read from the held leases instead of the
#: Job state or the fence's acceptance age, so the debt that aged out of the fourth
#: read is named again (#1008). It is a fifth scope and, like the fourth, never joins
#: the three ledger marks: it states which read named the Job, not what the ledger can
#: still advance. A Job is tagged here only when no other read named it, i.e. the
#: bounded read had stopped. The three shapes it catches are "the worker never claimed
#: the acceptance" (no ledger row at all), "the ledger is past its schedule" (unsettled,
#: outside the window) and "the ledger can never be claimed again" (``exhausted``) --
#: "abandoned" is about the read face's horizon, not a verdict: only ``never claimable
#: (exhausted)`` proves no automatic claim is left.
ABANDONED_HOLDING_CAPACITY = "abandoned-holding-capacity"
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
#: Default lookback of the left-cancelling read, in seconds. That read cannot use
#: ``speech_jobs_live_state_scan`` -- the partial index holds only ``running`` and
#: ``cancelling`` Jobs (migration 0056), which is exactly the state this class has
#: left -- so migration 0060 indexes the fence's acceptance age and this window is
#: the range the 5 s poll touches. It must exceed ``grace_seconds`` (a window no
#: longer than the grace could never contain an acceptance old enough to report) and
#: is deliberately far past both the confirmation window and the retry backoff: at
#: the probe's default 900 s grace, 24 h is 96x the grace, 288x the default 300 s
#: confirmation window and 288x the 300 s backoff ceiling. What happens to debt
#: older than the window is stated with ``_LEFT_CANCELLING_DEBT``.
_DEFAULT_LEFT_CANCELLING_WINDOW_SECONDS = 86_400
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

    A fourth scope names the Job-state side fact, not a ledger fact:
    ``left-cancelling-holding-capacity: N`` counts Jobs that already left
    ``cancelling`` while a lease they own still holds capacity and the ledger is
    unsettled. The state-gated read cannot see that shape, so this read starts from
    the unsettled acceptance and the held lease instead of the Job state. The label
    is reported *beside* the three marks and never folded into them: one Job can be
    both ``never claimed`` and ``left-cancelling-holding-capacity``, and the label
    counts Jobs whose Job row, not whose schedule, is the fact. The read is bounded
    by acceptance age (``left_cancelling_window_seconds``, default 24 h), which is a
    bound on alarm coverage: debt older than the window used to leave the detail
    entirely, with only the reconciliation worker (while the ledger still allows a
    claim) and the unbounded operator drill-down query still naming it. The fifth
    scope below is what closed that horizon.

    A fifth scope is the unbounded sibling of the fourth:
    ``abandoned-holding-capacity: N`` counts Jobs that hold capacity through a lease
    whose acceptance no bounded read names any more, read with **no** lookback window
    and no Job-state gate. It starts from the held leases (migration 0062's partial
    index over ``admission_leases``) instead of the Job state, the fence's acceptance
    age or the ledger row -- so its cost is bounded by the capacity that is reserved,
    a number the deployment cannot exceed, rather than by its cancellation history,
    its ledger history or its uptime. It therefore still names the oldest debt, which
    is the shape #1008 confirmed: ``state = 'cancelled'`` + ``lease_state =
    'reserved'`` + ``last_outcome = 'exhausted'`` with an acceptance older than the
    fourth read's window; and it also names the shape the ledger-driven attempt could
    not, where the worker never claimed the acceptance and there is no ledger row at
    all. A ``settled`` ledger row is excluded: the acceptance is closed, and capacity
    still held under it belongs to the release path. The label stays beside the fourth
    and never folds into it -- the fourth is bounded, this one is not, and an
    acceptance that ages out of the window moves from
    ``left-cancelling-holding-capacity`` to ``abandoned-holding-capacity``. It is not
    a verdict either: only ``never claimable (exhausted)`` proves that no automatic
    claim is left; an unsettled row stays claimable by the worker when reconciliation
    is enabled, and a row with no ledger row at all is one the worker never claimed.

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
    ``left_cancelling_window_seconds`` bounds only the fourth, age-indexed read. It
    must exceed ``grace_seconds``: every reported acceptance is already older than
    the grace, so a window no larger than that would always select nothing. The
    default (24 h) is far past the confirmation window plus backoff, so an
    acceptance is never reported before its judgement can complete. The fifth read
    takes no lookback window: it is index-bounded by the held leases, so nothing it
    names depends on how long the debt has existed.
    """

    def __init__(
        self,
        dsn: str | None,
        *,
        schema: str,
        timeout_seconds: float,
        grace_seconds: float = 900,
        left_cancelling_window_seconds: float = _DEFAULT_LEFT_CANCELLING_WINDOW_SECONDS,
        limit: int = 96,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
            raise ValueError("Invalid readiness schema")
        if not 0 < grace_seconds <= 86_400:
            raise ValueError("Cancellation debt grace must be bounded")
        if not grace_seconds < left_cancelling_window_seconds <= 7 * 86_400:
            raise ValueError("Left-cancelling read window must exceed the grace and stay bounded")
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
        self._window_us = int(left_cancelling_window_seconds * 1_000_000)
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
                rows = list(await cursor.fetchall())
                # The database clock is read once and passed to the age-indexed read so
                # its window, grace and ``due`` branches all measure the same instant and
                # a test can pin the window boundary. It never comes from the process.
                now_us = await self._database_now(cursor)
                await cursor.execute(
                    _LEFT_CANCELLING_DEBT,
                    {
                        "now_us": now_us,
                        "window_us": self._window_us,
                        "grace_us": self._grace_us,
                        "limit": self._limit,
                    },
                )
                rows.extend(await cursor.fetchall())
                # The fifth read shares the same ``now_us`` and grace. It takes no
                # lookback window, but its ``due`` branch, its grace premise and the
                # fourth read must judge the same acceptance at the same instant.
                await cursor.execute(
                    _PERMANENT_DEBT,
                    {"now_us": now_us, "grace_us": self._grace_us, "limit": self._limit},
                )
                rows.extend(await cursor.fetchall())
            except _UNREADABLE_TABLES as error:
                # The role is not granted the execution/lease tables (or the schema
                # never dispatched): Job names are still the actionable part, so
                # report them and say which detail is missing. The fourth and fifth
                # reads need the same grants, so they degrade with this one.
                await connection.rollback()
                rows = await self._job_level(cursor)
                await connection.rollback()
                return _describe_debt(rows, missing=_failure_category(error))
        # End the read transaction so the reused session stays idle between polls.
        await connection.rollback()
        return _describe_debt(rows)

    async def _database_now(self, cursor: psycopg.AsyncCursor[Any]) -> int:
        await cursor.execute(
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT)"
        )
        row = await cursor.fetchone()
        assert row is not None  # a clock read always returns one row
        return int(row[0])

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

    A row whose ninth column is :data:`LEFT_CANCELLING_HOLDING_CAPACITY` also
    contributes to the ``left-cancelling-holding-capacity`` scope. That scope is
    counted per Job from the Job row, appended after the three ledger marks, and is
    never one of them: the marks describe the ledger's schedule, the label describes
    the Job's state, and a Job may carry both.

    A row tagged :data:`ABANDONED_HOLDING_CAPACITY` contributes to its own scope, but
    only when no earlier read named that Job. The reads overlap by construction now
    that the fifth has neither a Job-state gate nor an age bound, so one lease must
    be counted once -- the first read that names it owns it -- and the fifth label
    then means exactly what it says: the age-bounded read face no longer names this
    Job. The two labels describe one read each and never merge, and the fifth stays
    out of ``schedule_jobs`` for the same reason the fourth does.
    """
    if not rows:
        return None
    jobs: dict[str, list[str]] = {}
    lease_counts: dict[str, int] = {}
    job_counts: dict[str, int] = {}
    schedule_jobs: dict[str, set[str]] = {state: set() for state, _ in _SCHEDULE_SCOPES}
    left_cancelling_jobs: set[str] = set()
    abandoned_jobs: set[str] = set()
    # ``(job, lease)`` is one held lease however many reads name it: counting it twice
    # would inflate both "over N held lease(s)" and its subclass count. The rows arrive
    # in read order, so the first (bounded, state-gated) read owns the lease and the
    # fifth only names what nothing else named.
    named: set[tuple[str, str]] = set()
    for row in rows:
        job_id, lease_id, lease_state, execution_state = row[0], row[1], row[2], row[3]
        debt_class = row[8] if len(row) > 8 else None
        # The state decides what can advance this acceptance, so it is counted per Job:
        # every lease row of one Job repeats the same ledger fact. The guard keeps
        # synthetic rows (and a query that does not carry the column) from inventing one.
        schedule = row[7] if len(row) > 7 else None
        if isinstance(schedule, str) and schedule in schedule_jobs:
            schedule_jobs[schedule].add(job_id)
        # The fourth read tags its own rows: this is the Job-state side fact, kept out
        # of ``schedule_jobs`` so it can never be counted as a ledger mark.
        if debt_class == LEFT_CANCELLING_HOLDING_CAPACITY:
            left_cancelling_jobs.add(job_id)
        if lease_id is None:
            # Job-level rows carry no lease identity: say so instead of inventing one.
            label = "no-held-lease" if missing is None else "lease-unread"
            jobs.setdefault(job_id, []).append(label)
            job_counts[label] = job_counts.get(label, 0) + 1
            continue
        if (job_id, lease_id) in named:
            continue
        named.add((job_id, lease_id))
        if debt_class == ABANDONED_HOLDING_CAPACITY:
            abandoned_jobs.add(job_id)
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
    if left_cancelling_jobs:
        # The Job-state fact is its own labelled scope, after the three ledger marks:
        # it answers "did this Job leave cancelling while still holding capacity", not
        # "what can the ledger advance", so it never appears as a fourth mark.
        scopes.append(f"{LEFT_CANCELLING_HOLDING_CAPACITY}: {len(left_cancelling_jobs)}")
    if abandoned_jobs:
        # And the unbounded read's own scope, last: it answers "did this debt outlive
        # the read face that was supposed to alarm on it", which is neither a mark nor
        # the Job-state label above.
        scopes.append(f"{ABANDONED_HOLDING_CAPACITY}: {len(abandoned_jobs)}")
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

# The fourth read: the same accepted cancellation and held-lease facts, with **no**
# Job-state gate. `_DEBT` above is gated on `state = 'cancelling'` because that is the
# only shape `speech_jobs_live_state_scan` (migration 0056) can reach; a Job that closed
# as `cancelled` after a refused release, or was failed by another owner while its
# lease stayed reserved, therefore held capacity without appearing in the detail -- the
# #852 residual blind spot. Removing the gate from the state-indexed reads would walk
# the whole Job table and the deployment's entire fence history on every 5 s poll, so
# this read gets its own bounded path instead:
#
#   * migration 0060 adds `speech_job_cancellation_fence_age_scan`, so the predicate
#     below is an index range on `accepted_at_us` and the cost follows the acceptances
#     accepted inside the window, never the settled history before it. Migration 0062
#     indexes the held leases, which gives the planner a second bounded path for this
#     read (lease side, then the fences by primary key); either path is an index, and
#     the plan tests accept both while still refusing a scan of the fence history;
#   * the window's lower edge is `now - window` and its upper edge is `now - grace`,
#     i.e. the same "old enough to judge" rule as `_DEBT`. Its default (24 h) is far
#     past the confirmation window plus backoff and must exceed `grace_seconds`, so it
#     can never report an acceptance whose judgement is still in flight.
#
# Why the lower edge is safe: a row older than the window is not advanced by this read
# in any case -- the probe is read-only. The actors that own that older debt do not use
# this window: the reconciliation worker's claim has an upper age bound only, so it
# keeps claiming old unsettled fences while the ledger still allows a claim (not
# `settled`, not `exhausted`); an `exhausted` row is the operator exit's. The operator
# drill-down query in `docs/components/production-jobs.md` is unbounded by
# construction. What the window used to cost was *alarm coverage*: after 24 h a
# still-held slot left this detail. The fifth read below is what closes that: it names
# the debt this window stops naming, so a missing label is no longer the operator's only
# cue and the drill-down query is a cross-check rather than the sole authority.
_LEFT_CANCELLING_DEBT = f"""
SELECT j.job_id, l.lease_id, l.lease_state, i.execution_state,
       e.evidence_kind, e.execution_stopped, e.resources_released,
       -- The ledger facts stay attached: they are facts about the same acceptance and
       -- the detail counts them per Job alongside the existing marks. The last column
       -- is the Job-state side fact, and is what `_describe_debt` counts separately.
       {_SCHEDULE_STATE_SQL} AS schedule_state,
       '{LEFT_CANCELLING_HOLDING_CAPACITY}' AS debt_class
FROM {job_cancellation_fence_schema.TABLE} AS f
JOIN {public_job_schema.JOBS} AS j ON j.job_id = f.job_id
LEFT JOIN {job_cancellation_fence_schema.RECONCILIATION_TABLE} AS r ON r.job_id = f.job_id
-- The database clock is read once by the probe and passed here, so the window, the
-- grace filter and the `due` branch above all measure the same instant.
CROSS JOIN (SELECT %(now_us)s::BIGINT AS now_us) AS now_clock
-- Same capacity rule as `_DEBT`: a lease that still holds capacity is the blocker.
JOIN admission_leases AS l
  ON l.operation_id = f.job_id
 AND l.lease_state NOT IN ('released', 'quarantined')
LEFT JOIN submit_intents AS i ON i.lease_id = l.lease_id
LEFT JOIN execution_stop_evidence AS e ON e.submit_intent_id = i.submit_intent_id
-- Disjoint from `_DEBT` by construction: a Job still `cancelling` is that read's row.
WHERE j.state <> 'cancelling'
  AND (r.job_id IS NULL OR r.settled_at_us IS NULL)
  AND f.accepted_at_us >= now_clock.now_us - %(window_us)s
  AND f.accepted_at_us <= now_clock.now_us - %(grace_us)s
ORDER BY f.accepted_at_us, f.job_id, l.lease_id
LIMIT %(limit)s
"""

# The fifth read: the same acceptance-and-held-lease fact with **no lookback window**, which is
# what makes it see the permanent debt (#1008). A Job that closed as `cancelled` while its
# release was refused holds capacity that
#
#   * `_DEBT` cannot see -- the Job is no longer `cancelling`;
#   * `_LEFT_CANCELLING_DEBT` cannot see once the acceptance is older than its window,
#     which is exactly the oldest and worst debt;
#   * and no read sees at all when the worker never claimed the acceptance, so there is
#     no ledger row to start from (`_LEFT_CANCELLING_DEBT`'s ledger join is LEFT, but its
#     window still excludes the old ones).
#
# So the bound comes from the debt itself: the leases that still hold capacity. That set is
# bounded by the deployment's instance slots -- capacity it cannot exceed, since a lease is
# what reserves that capacity -- so it does not grow with the ledger, the fence table, the
# Job table or uptime. Migration 0062 adds `speech_job_cancellation_lease_debt_scan`, a
# partial index on `admission_leases (operation_id) WHERE lease_state NOT IN ('released',
# 'quarantined')`, and this read starts there.
#
# Why not the ledger (migration 0061's first attempt, superseded): an `exhausted` row keeps
# `settled_at_us IS NULL` forever -- nothing in the codebase ever settles it -- so every
# exhausted acceptance after a refused release is a *permanent* member of that partial index
# even when its lease was released long ago. The index therefore grows with the deployment's
# failures, not with the capacity still held: measured 0.058 ms with 10 open rows, 0.879 ms
# with 100 exhausted history rows (the read falls back to a sequential scan of the fence
# table), 1.204 ms with 2 000. The lease-driven read measures 0.06-0.08 ms on all three.
#
# Why not the fence table, and why the lease is the right driver: `_LEFT_CANCELLING_DEBT` is
# fence-driven and needs its window for exactly that reason -- `speech_job_cancellation_fences`
# keeps one row per accepted cancellation for the deployment's whole life. A held lease, by
# contrast, is a live resource, and the capacity it consumes is what makes the debt matter.
#
# The ledger is a LEFT JOIN, not the source: a cancellation the worker never claimed has no
# ledger row at all, and that shape (accepted, released refusal, no ledger) is one of the
# three the bounded reads cannot name. `settled` rows are excluded: the ledger says the
# acceptance is closed, so a lease that still holds capacity in that shape is the release
# path's business, not this diagnostic's.
_PERMANENT_DEBT = f"""
SELECT j.job_id, l.lease_id, l.lease_state, i.execution_state,
       e.evidence_kind, e.execution_stopped, e.resources_released,
       -- Same ledger facts and same last column as the fourth read: the three marks and
       -- the two labels describe one sentence, so no read may classify a row its own way.
       {_SCHEDULE_STATE_SQL} AS schedule_state,
       '{ABANDONED_HOLDING_CAPACITY}' AS debt_class
-- The held leases are the driving set and the bound: migration 0062's partial index holds
-- exactly these rows, and their number is bounded by the instance slots that reserve them.
FROM admission_leases AS l
-- The database clock is read once by the probe and passed here, so the `due` branch
-- above judges the schedule at the same instant as the fourth read.
CROSS JOIN (SELECT %(now_us)s::BIGINT AS now_us) AS now_clock
-- The Job row is not filtered (there is no state gate here), but it must exist and the
-- detail prints its id, exactly as in the other reads.
JOIN {public_job_schema.JOBS} AS j ON j.job_id = l.operation_id
-- Only an accepted cancellation is cancellation debt: a lease held by a Job whose
-- cancellation was never requested is a different fact, and the other probe reports it.
JOIN {job_cancellation_fence_schema.TABLE} AS f ON f.job_id = l.operation_id
-- LEFT, not JOIN: "the worker never claimed this acceptance" is one of the shapes this
-- read exists for, and an inner join would silently drop it.
LEFT JOIN {job_cancellation_fence_schema.RECONCILIATION_TABLE} AS r
  ON r.job_id = l.operation_id
LEFT JOIN submit_intents AS i ON i.lease_id = l.lease_id
LEFT JOIN execution_stop_evidence AS e ON e.submit_intent_id = i.submit_intent_id
-- Stated exactly as migration 0062's partial index states it, so the planner proves the
-- implication and reads the index instead of the whole lease table.
WHERE l.lease_state NOT IN ('released', 'quarantined')
  -- A closed acceptance is not this debt, whatever its lease still holds.
  AND (r.job_id IS NULL OR r.settled_at_us IS NULL)
  -- Only the shared premise, and only as an upper bound: the acceptance is past the
  -- grace every cancellation read requires. There is deliberately no lower bound --
  -- no lookback window -- which is the whole difference from the fourth read.
  AND f.accepted_at_us <= now_clock.now_us - %(grace_us)s
ORDER BY f.accepted_at_us, j.job_id, l.lease_id
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
