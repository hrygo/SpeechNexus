"""Bounded runtime transactions; ambiguous commits never trigger a fresh write."""

from __future__ import annotations

import random
import time
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.runtime_intents import (
    CreateRuntimeIntent,
    RuntimeIdentity,
    RuntimeRejectedError,
    RuntimeResult,
    rejected,
    unknown,
)
from speechnexus_backend.speech.domain.runtime_observations import ObserveRuntimeIntent
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.runtime_codec import prepare_request
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import decode_observation
from speechnexus_backend.speech.infrastructure.runtime_observation_sql import observe_in_transaction
from speechnexus_backend.speech.infrastructure.runtime_sql import (
    RuntimeCursor,
    create_in_transaction,
    query_in_transaction,
)


class _BudgetExpiredError(Exception):
    """Confirmed rollback before attempting commit."""


class _DeadlineCursor:
    def __init__(self, cursor: RuntimeCursor, deadline: float) -> None:
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


class PostgresRuntimeTransactions:
    def __init__(
        self, pool: SpeechPool, *, timeout_seconds: float = 3, max_attempts: int = 3
    ) -> None:
        if not 0 < timeout_seconds <= 30 or not 1 <= max_attempts <= 3:
            raise ValueError("Runtime transactions need bounded deadlines and retries")
        self._pool = pool
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts

    def _run(
        self,
        identity: RuntimeIdentity,
        command: CreateRuntimeIntent | ObserveRuntimeIntent | None = None,
        *,
        request_digest: str | None = None,
    ) -> RuntimeResult:
        prepared = (
            prepare_request(command.request) if isinstance(command, CreateRuntimeIntent) else None
        )
        observed = (
            decode_observation(command.original_document)
            if isinstance(command, ObserveRuntimeIntent)
            else None
        )
        if (
            observed is not None
            and isinstance(command, ObserveRuntimeIntent)
            and observed.observation != command.observation
        ):
            raise ValueError("Runtime observation facts do not match their original document")
        if prepared is not None:
            request_digest = prepared.request_digest
        deadline = time.monotonic() + self._timeout
        conflict_if_absent = False
        for attempt in range(self._max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with self._pool.connection(timeout=remaining) as connection:
                    if time.monotonic() >= deadline:
                        raise _BudgetExpiredError
                    if command is None or conflict_if_absent:
                        connection.execute("SET TRANSACTION READ ONLY")
                    with connection.cursor() as raw:
                        cursor = _DeadlineCursor(cast(RuntimeCursor, raw), deadline)
                        if command is None or conflict_if_absent:
                            result = query_in_transaction(
                                cursor,
                                identity,
                                request_digest=request_digest,
                                conflict_if_absent=conflict_if_absent,
                            )
                        elif isinstance(command, CreateRuntimeIntent):
                            assert prepared is not None
                            result = create_in_transaction(
                                cursor, command, prepared, clock=cursor.clock
                            )
                        else:
                            assert observed is not None
                            result = observe_in_transaction(
                                cursor, command, observed, clock=cursor.clock
                            )
                        if time.monotonic() >= deadline:
                            raise _BudgetExpiredError
                return result  # Only after commit acknowledgement and pool release.
            except RuntimeRejectedError as error:
                return rejected(identity, error.code)
            except psycopg.errors.UniqueViolation:
                if isinstance(command, ObserveRuntimeIntent):
                    return rejected(identity, "reconciliation_required")
                # The failed transaction has rolled back. Read the original
                # identity; a different head winner does not authorize a rewrite.
                conflict_if_absent = True
            except psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure:
                delay = min(random.uniform(0.001, 0.01), max(0, deadline - time.monotonic()))
                if delay and attempt + 1 < self._max_attempts:
                    time.sleep(delay)  # Outside the transaction and pool checkout.
            except PoolClosed, PoolTimeout, TooManyRequests, _BudgetExpiredError:
                break
            except psycopg.OperationalError, psycopg.InterfaceError:
                return unknown(identity)
            except psycopg.Error, ValueError, TypeError, KeyError:
                return rejected(identity, "reconciliation_required")
        return RuntimeResult(
            "rejected", identity, error_code="reconciliation_required", recovery="retry_transaction"
        )

    def create_runtime_intent(self, command: CreateRuntimeIntent) -> RuntimeResult:
        return self._run(command.request.identity, command)

    def query_runtime_intent(
        self, identity: RuntimeIdentity, *, request_digest: str | None = None
    ) -> RuntimeResult:
        return self._run(identity, request_digest=request_digest)

    def observe_runtime(self, command: ObserveRuntimeIntent) -> RuntimeResult:
        return self._run(command.observation.identity, command)
