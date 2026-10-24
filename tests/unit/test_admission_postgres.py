"""Bounded failure behavior at the PG adapter seam; real commits are tested separately."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.admission import unknown
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from tests.fake.admission import COMMAND
from tests.unit.test_runtime_postgres import FaultPool


def _port(pool: Any, **changes: Any) -> PostgresAdmissionTransactions:
    return PostgresAdmissionTransactions(
        cast(SpeechPool, pool), max_command_bytes=1048576, **changes
    )


@pytest.mark.parametrize("error", [psycopg.OperationalError(), psycopg.InterfaceError()])
@pytest.mark.parametrize("query", [False, True])
def test_ambiguous_transport_never_repeats_a_write(error: Exception, query: bool) -> None:
    pool = FaultPool(error)
    result = (
        _port(pool).query_admission(COMMAND.request.identity)
        if query
        else _port(pool).admit_recipe(COMMAND)
    )
    assert result.outcome == "unknown" and result.recovery == "query_original"
    assert pool.calls == 1 and not pool.active


@pytest.mark.parametrize(
    "error", [psycopg.errors.DeadlockDetected(), psycopg.errors.SerializationFailure()]
)
def test_confirmed_abort_retries_only_after_returning_the_connection(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = FaultPool(error)
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        assert not pool.active
        pauses.append(seconds)

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.admission_postgres.time.sleep", pause
    )
    result = _port(pool).admit_recipe(COMMAND)
    assert pool.calls == 3 and len(pauses) == 2
    assert result.recovery == "retry_transaction"


def test_unique_collision_can_only_attempt_read_only_original_recovery() -> None:
    pool = FaultPool(psycopg.errors.UniqueViolation())
    result = _port(pool).admit_recipe(COMMAND)
    assert pool.calls == 2 and pool.statements == ["SET TRANSACTION READ ONLY"]
    assert result.error_code == "reconciliation_required"


@pytest.mark.parametrize(
    "error", [PoolClosed(), PoolTimeout(), TooManyRequests(), psycopg.errors.QueryCanceled()]
)
def test_backpressure_and_confirmed_timeout_do_not_fabricate_reservations(error: Exception) -> None:
    pool = FaultPool(error)
    result = _port(pool).admit_recipe(COMMAND)
    assert result.fact is None and result.recovery == "retry_transaction"
    assert pool.calls == 1


@pytest.mark.parametrize(
    "error", [psycopg.errors.CheckViolation(), ValueError(), TypeError(), KeyError()]
)
def test_inconsistent_storage_fails_closed(error: Exception) -> None:
    pool = FaultPool(error)
    result = _port(pool).admit_recipe(COMMAND)
    assert result.error_code == "reconciliation_required" and result.fact is None
    assert pool.calls == 1


def test_invalid_or_oversize_private_context_never_checks_out_a_connection() -> None:
    pool = FaultPool(AssertionError("No SQL may run"))
    port = PostgresAdmissionTransactions(cast(SpeechPool, pool), max_command_bytes=1)
    assert port.admit_recipe(COMMAND).error_code == "invalid_execution_request"
    assert (
        _port(pool).query_admission(COMMAND.request.identity, request_digest="invalid").error_code
        == "invalid_execution_request"
    )
    assert pool.calls == 0


def test_expired_queue_budget_prevents_the_first_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]

    class Delayed:
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            now[0] = 2
            yield object()

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.admission_postgres.time.monotonic",
        lambda: now[0],
    )
    result = _port(Delayed(), timeout_seconds=1).admit_recipe(COMMAND)
    assert result.recovery == "retry_transaction"


def test_missing_original_query_is_read_only_and_remains_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Connection:
        def execute(self, query: str) -> None:
            statements.append(query)

        def cursor(self) -> Any:
            return nullcontext(object())

    class Pool:
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            yield Connection()

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.admission_postgres.read_original",
        lambda *args, **kwargs: None,
    )
    result = _port(Pool()).query_admission(COMMAND.request.identity)
    assert result == unknown(COMMAND.request.identity)
    assert statements == ["SET TRANSACTION READ ONLY"]


@pytest.mark.parametrize("error", [psycopg.errors.QueryCanceled(), PoolClosed(), ValueError()])
def test_error_after_work_finishes_cannot_claim_confirmed_rollback(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[object] = []

    class Connection:
        def cursor(self) -> Any:
            return nullcontext(object())

    class Pool:
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            yield Connection()
            raise error  # The commit reply/connection return is ambiguous.

    def work(*args: Any, **kwargs: Any) -> Any:
        writes.append(args)
        return unknown(COMMAND.request.identity)

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.admission_postgres.admit_in_transaction", work
    )
    result = _port(Pool()).admit_recipe(COMMAND)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    assert len(writes) == 1


@pytest.mark.parametrize("timeout,attempts", [(0, 3), (31, 3), (3, 0), (3, 4)])
def test_configuration_does_not_allow_unbounded_transactions(timeout: float, attempts: int) -> None:
    with pytest.raises(ValueError):
        _port(None, timeout_seconds=timeout, max_attempts=attempts)
