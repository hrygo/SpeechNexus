"""The reconciliation transaction installs one fixed server-side statement budget.

``PostgresCancellationReconciliationStore._run`` runs
``SELECT set_config('statement_timeout', ...)`` from ``timeout_seconds`` before
handing the connection to ``work``. The #1011 review measured that deleting
those two lines leaves the reconciliation suites green, so this file pins the
call itself and the three semantics that are easy to lose: it reaches the wire
before ``work``, the millisecond value is derived from the configured budget,
and a sub-millisecond budget floors at ``1`` instead of ``0``.

The budget is fixed for the whole transaction, not decremented per statement: a
``work`` closure that issues two statements receives ``set_config`` exactly
once, so both statements run under the full configured deadline. ``test_..._once_...``
below is the contract; a future move to a decreasing budget must change it
explicitly.

These tests never open a database; the fake pool and connection only record
what the adapter asks PostgreSQL to do.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pytest

from speechnexus_backend.speech.infrastructure.job_cancellation_reconciliation_postgres import (
    _RECORD_SETTLED,
    PostgresCancellationReconciliationStore,
    ReconciliationStoreUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

SET_TIMEOUT = "SELECT set_config('statement_timeout', ?, true)"
NOW = 1_700_000_000_000_000


class RecordingConnection:
    """Records every statement the adapter sends; returns an empty result set."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, query: str, parameters: object = ()) -> RecordingConnection:
        self.calls.append((query, parameters))
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class FakePool:
    def __init__(self) -> None:
        self.connection_ = RecordingConnection()
        self.timeouts: list[float] = []

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[RecordingConnection]:
        self.timeouts.append(timeout)
        yield self.connection_


def build(timeout_seconds: float = 3) -> tuple[PostgresCancellationReconciliationStore, FakePool]:
    pool = FakePool()
    store = PostgresCancellationReconciliationStore(
        cast(SpeechPool, pool), timeout_seconds=timeout_seconds
    )
    return store, pool


def test_statement_timeout_is_set_before_work_from_the_configured_budget() -> None:
    store, pool = build(timeout_seconds=1.5)

    store.record_settled("job-1", now_us=NOW)

    assert pool.timeouts == [1.5], "the pool acquisition must use the bounded budget too"
    assert pool.connection_.calls == [
        (SET_TIMEOUT, ("1500",)),
        (_RECORD_SETTLED, (NOW, NOW, NOW, "job-1")),
    ]


def test_transaction_installs_statement_timeout_once_for_multiple_statements() -> None:
    """The budget is installed once per transaction, not once per statement.

    No public method issues two statements today, so this case pins the intent for the
    first `work` that does: `statement_timeout` is a per-statement server setting, so
    re-installing it per statement would silently turn a 2 s budget into 2 s x N.
    `_run` is called directly because the public entry points cannot express it.
    """
    store, pool = build(timeout_seconds=2)

    def work(connection: Any) -> list[dict[str, Any]]:
        first = connection.execute("SELECT 1").fetchall()
        second = connection.execute("SELECT 2").fetchall()
        return [*first, *second]

    assert store._run(work) == []

    queries = [query for query, _ in pool.connection_.calls]
    assert queries == [SET_TIMEOUT, "SELECT 1", "SELECT 2"]
    assert queries.count(SET_TIMEOUT) == 1, (
        "the statement budget is fixed for the transaction; a second statement must not "
        f"re-install it or receive a decremented one: {queries!r}"
    )


def test_sub_millisecond_budget_floors_at_one_millisecond_not_zero() -> None:
    """`max(1, ...)` keeps the milliseconds an integer YAML-free string, never "0".

    This is the arithmetic guard, not a reachable production path: psycopg_pool turns
    a sub-millisecond `timeout` into an immediate PoolTimeout before `_run` gets a
    connection, so the value is only defended, never observed.
    """
    timeout_seconds = 1e-7
    assert 0 < timeout_seconds <= 30, "the constructor must accept this budget as-is"
    store, pool = build(timeout_seconds=timeout_seconds)

    store.record_settled("job-1", now_us=NOW)

    assert pool.connection_.calls[0] == (SET_TIMEOUT, ("1",))


def test_storage_error_is_wrapped_with_its_original_cause() -> None:
    store, pool = build()
    failure = psycopg.OperationalError("synthetic connection loss")

    def work(connection: Any) -> None:
        raise failure

    with pytest.raises(ReconciliationStoreUnavailableError) as raised:
        store._run(work)

    assert raised.value.__cause__ is failure
    assert pool.connection_.calls == [(SET_TIMEOUT, ("3000",))]
