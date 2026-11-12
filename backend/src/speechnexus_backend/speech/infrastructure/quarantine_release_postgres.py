"""Bounded PostgreSQL port for the operator quarantine release transaction."""

from __future__ import annotations

import random
import time
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.quarantine_releases import (
    QuarantineUnavailableError,
    QuarantineUnknownError,
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
    quarantine_release_in_transaction,
    read_ledger,
)


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
        except psycopg.Error, ValueError, TypeError, KeyError:
            raise QuarantineReleaseRejectedError("quarantine_release_conflict") from None
        if ledger is None:
            return None
        outcome = decide_replay(ledger, evidence)
        if outcome != "replay":
            raise QuarantineReleaseRejectedError(outcome)
        return ReleaseOutcome(ledger, True)

    def commit(self, evidence: ReleaseEvidence) -> ReleaseOutcome:
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
                        outcome = quarantine_release_in_transaction(
                            cursor, evidence, clock=cursor.clock
                        )
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
            except psycopg.Error, ValueError, TypeError, KeyError:
                raise QuarantineReleaseRejectedError("quarantine_release_conflict") from None
        raise QuarantineUnavailableError from None


__all__ = ["PostgresQuarantineReleases"]
