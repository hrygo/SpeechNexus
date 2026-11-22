"""Bounded PostgreSQL port for the operator quarantine release transaction."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.completed_releases import (
    CompletedLeaseSnapshot,
)
from speechnexus_backend.speech.application.quarantine_releases import (
    QuarantineSurfaceError,
    QuarantineUnavailableError,
    QuarantineUnknownError,
)
from speechnexus_backend.speech.domain.completed_release import (
    CompletedStopEvidence,
    DrainRecord,
)
from speechnexus_backend.speech.domain.leases import LeaseRejectedError
from speechnexus_backend.speech.domain.quarantine_release import (
    QuarantineReleaseRejectedError,
    ReleaseEvidence,
    ReleaseOutcome,
    decide_replay,
)
from speechnexus_backend.speech.infrastructure.leases_sql import LeaseCursor
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.quarantine_release_sql import (
    completed_release_in_transaction,
    quarantine_release_in_transaction,
    read_ledger,
)
from speechnexus_portable_sql.clock import ClockReader, now_us

#: A missing relation, column or schema object means the migration chain did not
#: reach 0052 in this database; the fix is `speechnexus-job-migrate`, not new
#: evidence. ``InvalidSchemaName`` covers a mis-set ``SPEECHNEXUS_DATABASE_SCHEMA``.
_MISSING_OBJECT = (
    psycopg.errors.UndefinedTable,
    psycopg.errors.UndefinedColumn,
    psycopg.errors.UndefinedObject,
    psycopg.errors.UndefinedFunction,
    psycopg.errors.InvalidSchemaName,
)


def _surface_defect(error: BaseException) -> str:
    """Name a storage refusal instead of reporting it as a business conflict.

    Only a genuine state mismatch may become 409: a lease that is not
    quarantined, evidence that does not match, an idempotency key owned by
    another request. A missing table or a revoked grant is a deployment defect,
    and reporting it as ``quarantine_release_conflict`` would tell the operator
    to change evidence or escalate a lease that is not the problem.
    """
    if isinstance(error, psycopg.errors.InsufficientPrivilege):
        return "quarantine_release_privilege_missing"
    if isinstance(error, _MISSING_OBJECT):
        return "quarantine_release_schema_not_migrated"
    return "quarantine_release_failed"


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
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self._cursor.execute(query, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._cursor.fetchall()

    def fetchone(self) -> dict[str, Any] | None:
        rows = self.fetchall()
        return rows[0] if rows else None

    def clock(self) -> int:
        # 「现在」只从便携时钟读一次;语句里不再有品牌函数。
        now: int = now_us(cast(ClockReader, self))
        return now


_SNAPSHOT = (
    "SELECT l.lease_id, l.operation_id, l.attempt_id, l.stage_id, l.instance_id, "
    "l.instance_generation, l.lease_state, l.cleanup_state, l.budget_released_at_us, "
    "l.quarantine_observed_at_us, "
    "i.submit_intent_id, i.execution_state, i.cleanup_state AS execution_cleanup_state, "
    "i.recorded_at_us, i.dispatched_at_us, i.terminated_at_us, i.version AS intent_version, "
    "e.evidence_kind, e.observed_at_us AS stop_observed_at_us, e.execution_stopped, "
    "e.resources_released, n.held_slots AS instance_held_slots, "
    "n.quarantined_slots AS instance_quarantined_slots, "
    "f.accepted_at_us AS fence_accepted_at_us, r.settled_at_us AS reconciliation_settled_at_us, "
    "j.state AS job_state "
    "FROM admission_leases AS l "
    "LEFT JOIN submit_intents AS i ON i.lease_id = l.lease_id "
    "LEFT JOIN execution_stop_evidence AS e ON e.submit_intent_id = i.submit_intent_id "
    "LEFT JOIN engine_instances AS n ON n.instance_id = l.instance_id "
    "AND n.instance_generation = l.instance_generation "
    "LEFT JOIN speech_jobs AS j ON j.job_id = l.operation_id "
    "LEFT JOIN speech_job_cancellation_fences AS f ON f.job_id = l.operation_id "
    "LEFT JOIN speech_job_cancellation_reconciliations AS r ON r.job_id = l.operation_id "
    "WHERE l.lease_id = ?"
)


def _read_completed_snapshot(cursor: LeaseCursor, lease_id: str) -> CompletedLeaseSnapshot | None:
    """One read-only statement: the lease, its intent, its terminal fact, counters."""
    cursor.execute(_SNAPSHOT, (lease_id,))
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        # A lease carries one intent and one terminal fact. More than one row is a
        # repair problem: picking ``rows[0]`` would choose which fact to believe.
        raise QuarantineReleaseRejectedError("reconciliation_required")
    row = rows[0]
    stop = (
        None
        if row["evidence_kind"] is None
        else CompletedStopEvidence(
            row["evidence_kind"],
            row["stop_observed_at_us"],
            row["execution_stopped"],
            row["resources_released"],
        )
    )
    return CompletedLeaseSnapshot(
        lease_id=row["lease_id"],
        operation_id=row["operation_id"],
        attempt_id=row["attempt_id"],
        stage_id=row["stage_id"],
        instance_id=row["instance_id"],
        instance_generation=int(row["instance_generation"]),
        lease_state=row["lease_state"],
        lease_cleanup_state=row["cleanup_state"],
        budget_released_at_us=row["budget_released_at_us"],
        quarantine_observed_at_us=row["quarantine_observed_at_us"],
        submit_intent_id=row["submit_intent_id"],
        execution_state=row["execution_state"],
        execution_cleanup_state=row["execution_cleanup_state"],
        recorded_at_us=row["recorded_at_us"],
        dispatched_at_us=row["dispatched_at_us"],
        terminated_at_us=row["terminated_at_us"],
        intent_version=row["intent_version"],
        stop=stop,
        fence_accepted_at_us=row["fence_accepted_at_us"],
        reconciliation_settled_at_us=row["reconciliation_settled_at_us"],
        job_state=row["job_state"],
        instance_held_slots=int(row["instance_held_slots"]),
        instance_quarantined_slots=int(row["instance_quarantined_slots"]),
    )


class PostgresQuarantineReleases:
    """Level-one ledger replay plus the bounded level-two release transaction.

    A committed ledger row is immutable, so the replay read never takes a
    business lock: a polling operator script must not compete with admission for
    pool or host row locks. Only a first release enters the write transaction,
    which retries solely on database-confirmed deadlock/serialization rollbacks.
    """

    def __init__(
        self, pool: SpeechPool, *, timeout_seconds: float = 3, max_attempts: int = 3
    ) -> None:
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Quarantine release requires bounded deadlines and retries")
        self._pool = pool
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts

    def lookup(self, evidence: ReleaseEvidence) -> ReleaseOutcome | None:
        """Read the immutable ledger without any business lock."""
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=max(0.001, deadline - time.monotonic())) as conn:
                if time.monotonic() >= deadline:
                    raise _BudgetExpiredError
                conn.execute("SET TRANSACTION READ ONLY")
                with conn.cursor() as raw:
                    cursor = _DeadlineCursor(cast(LeaseCursor, raw), deadline)
                    ledger = read_ledger(cursor, evidence.lease_id)
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
        except PoolClosed, PoolTimeout, TooManyRequests, _BudgetExpiredError:
            raise QuarantineUnavailableError from None
        except psycopg.OperationalError, psycopg.InterfaceError:
            raise QuarantineUnavailableError from None
        except (psycopg.Error, ValueError, TypeError, KeyError) as error:
            # The read-only transaction confirmed rollback; a broken read surface
            # is never a lease conflict.
            raise QuarantineSurfaceError(_surface_defect(error)) from None
        if ledger is None:
            return None
        outcome = decide_replay(ledger, evidence)
        if outcome != "replay":
            raise QuarantineReleaseRejectedError(outcome)
        return ReleaseOutcome(ledger, True)

    def commit(self, evidence: ReleaseEvidence) -> ReleaseOutcome:
        return self._commit(
            lambda cursor: quarantine_release_in_transaction(cursor, evidence, clock=cursor.clock)
        )

    def commit_completed(self, evidence: ReleaseEvidence, drain: DrainRecord) -> ReleaseOutcome:
        """Level two for a completed execution; the same ledger, a different fact."""
        return self._commit(
            lambda cursor: completed_release_in_transaction(
                cursor, evidence, drain, clock=cursor.clock
            )
        )

    def completed_snapshot(self, lease_id: str) -> CompletedLeaseSnapshot | None:
        """Read one lease, its intent, its terminal fact and its instance counters.

        A read-only single statement set under the release identity's own grants:
        the operator tool identifies the target, prints the capacity it is about
        to return and verifies the result without a second DSN or the API role.
        """
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=max(0.001, deadline - time.monotonic())) as conn:
                if time.monotonic() >= deadline:
                    raise _BudgetExpiredError
                conn.execute("SET TRANSACTION READ ONLY")
                with conn.cursor() as raw:
                    cursor = _DeadlineCursor(cast(LeaseCursor, raw), deadline)
                    snapshot = _read_completed_snapshot(cursor, lease_id)
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
        except PoolClosed, PoolTimeout, TooManyRequests, _BudgetExpiredError:
            raise QuarantineUnavailableError from None
        except psycopg.OperationalError, psycopg.InterfaceError:
            raise QuarantineUnavailableError from None
        except (psycopg.Error, ValueError, TypeError, KeyError) as error:
            raise QuarantineSurfaceError(_surface_defect(error)) from None
        return snapshot

    def _commit(self, decide: Callable[[_DeadlineCursor], ReleaseOutcome]) -> ReleaseOutcome:
        deadline = time.monotonic() + self._timeout
        for attempt in range(self._max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with self._pool.connection(timeout=remaining) as connection:
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
                    with connection.cursor() as raw:
                        cursor = _DeadlineCursor(cast(LeaseCursor, raw), deadline)
                        outcome = decide(cursor)
                        if time.monotonic() >= deadline:
                            raise _BudgetExpiredError
                return outcome  # Commit acknowledgement precedes success.
            except QuarantineReleaseRejectedError:
                raise
            except psycopg.errors.UniqueViolation:
                raise QuarantineReleaseRejectedError("quarantine_release_conflict") from None
            except LeaseRejectedError as error:
                code = "not_found" if error.code == "not_found" else "quarantine_release_conflict"
                raise QuarantineReleaseRejectedError(code) from None
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                delay = min(random.uniform(0.001, 0.01), max(0, deadline - time.monotonic()))
                if delay and attempt + 1 < self._max_attempts:
                    time.sleep(delay)  # Confirmed rollback; no checked-out connection.
            except PoolClosed, PoolTimeout, TooManyRequests, _BudgetExpiredError:
                raise QuarantineUnavailableError from None
            except psycopg.OperationalError, psycopg.InterfaceError:
                raise QuarantineUnknownError from None  # Never repeat an ambiguous write.
            except (psycopg.Error, ValueError, TypeError, KeyError) as error:
                # The connection context rolled back, so nothing was released;
                # a missing table, a revoked grant or an unexpected storage
                # refusal is a deployment defect, not an evidence conflict.
                raise QuarantineSurfaceError(_surface_defect(error)) from None
        raise QuarantineUnavailableError from None


__all__ = ["PostgresQuarantineReleases"]
