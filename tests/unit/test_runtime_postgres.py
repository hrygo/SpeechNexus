"""Failure classification and bounded transaction behavior of runtime PG adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.runtime_postgres import (
    PostgresRuntimeTransactions,
    _BudgetExpiredError,
    _DeadlineCursor,
)
from tests.fake.runtime_intents import COMMAND, REQUEST


class FaultPool:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0
        self.active = False
        self.statements: list[str] = []

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Any]:
        self.calls += 1
        self.active = True
        pool = self

        class Connection:
            def execute(self, query: str) -> None:
                pool.statements.append(query)
                raise pool.error

            def cursor(self) -> Any:
                raise pool.error

        try:
            yield Connection()
        finally:
            self.active = False


@pytest.mark.parametrize("error", [psycopg.OperationalError(), psycopg.InterfaceError()])
@pytest.mark.parametrize("query", [False, True])
def test_transport_ambiguity_does_not_repeat_write_or_query(error: Exception, query: bool) -> None:
    pool = FaultPool(error)
    port = PostgresRuntimeTransactions(cast(SpeechPool, pool))
    result = (
        port.query_runtime_intent(REQUEST.identity)
        if query
        else port.create_runtime_intent(COMMAND)
    )
    assert result.outcome == "unknown" and result.recovery == "query_original" and pool.calls == 1


@pytest.mark.parametrize(
    "error", [psycopg.errors.DeadlockDetected(), psycopg.errors.SerializationFailure()]
)
def test_only_confirmed_aborts_retry_after_pool_release(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = FaultPool(error)
    sleeps: list[float] = []

    def pause(delay: float) -> None:
        assert not pool.active
        sleeps.append(delay)

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.runtime_postgres.time.sleep", pause
    )
    result = PostgresRuntimeTransactions(cast(SpeechPool, pool)).create_runtime_intent(COMMAND)
    assert pool.calls == 3 and len(sleeps) == 2 and result.recovery == "retry_transaction"


def test_unique_conflict_switches_to_read_only_original_identity() -> None:
    pool = FaultPool(psycopg.errors.UniqueViolation())
    result = PostgresRuntimeTransactions(cast(SpeechPool, pool)).create_runtime_intent(COMMAND)
    assert pool.calls == 3 and pool.statements == ["SET TRANSACTION READ ONLY"] * 2
    assert result.outcome == "rejected"


@pytest.mark.parametrize("error", [PoolClosed(), PoolTimeout(), TooManyRequests()])
def test_backpressure_never_fabricates_runtime(error: Exception) -> None:
    pool = FaultPool(error)
    result = PostgresRuntimeTransactions(cast(SpeechPool, pool)).create_runtime_intent(COMMAND)
    assert pool.calls == 1 and result.recovery == "retry_transaction"


def test_expired_queue_budget_prevents_first_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]

    class DelayedPool:
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            now[0] = 2
            yield object()

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.runtime_postgres.time.monotonic", lambda: now[0]
    )
    result = PostgresRuntimeTransactions(
        cast(SpeechPool, DelayedPool()), timeout_seconds=1
    ).create_runtime_intent(COMMAND)
    assert result.recovery == "retry_transaction"


def test_each_statement_receives_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1.0]
    statements: list[tuple[str, tuple[object, ...]]] = []

    class Raw:
        rowcount = 1

        def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
            statements.append((query, parameters))

        def fetchall(self) -> list[dict[str, Any]]:
            return [{"now_us": 1000}]

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.runtime_postgres.time.monotonic", lambda: now[0]
    )
    cursor = _DeadlineCursor(Raw(), 2)
    assert cursor.clock() == 1000 and cursor.rowcount == 1
    assert statements[0][1] == ("1000",)
    now[0] = 1.75
    cursor.execute("SELECT 1")
    assert statements[2][1] == ("250",)
    now[0] = 2
    with pytest.raises(_BudgetExpiredError):
        cursor.execute("SELECT 2")
    assert len(statements) == 4


@pytest.mark.parametrize("timeout,attempts", [(0, 3), (31, 3), (3, 0), (3, 4)])
def test_configuration_must_be_bounded(timeout: float, attempts: int) -> None:
    with pytest.raises(ValueError):
        PostgresRuntimeTransactions(
            cast(SpeechPool, None), timeout_seconds=timeout, max_attempts=attempts
        )
