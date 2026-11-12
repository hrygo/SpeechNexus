"""Storage projection of one audit page: statement shape, rows and failure mapping.

The statement is the only query this reader may run, so the cases pin its exact
predicates and ordering, then prove that any storage failure, deadline overrun
or untrusted stored summary becomes one typed unavailability instead of a
partial or mislabeled page.
"""

from __future__ import annotations

import time
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.audit_events import (
    AuditEventPosition,
    AuditEventQuery,
    AuditEventUnavailableError,
)
from speechnexus_backend.speech.infrastructure.audit_events_postgres import (
    PostgresAuditEventReads,
    statement,
)

POSITION = (1_700_000_000_000_005, "lifecycle-00000000000000000000000000000005")


def row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event_id": "lifecycle-00000000000000000000000000000001",
        "kind": "binding",
        "action": "model_binding.retire",
        "binding_id": "asr-quality",
        "request_id": "request-audit-1",
        "actor_type": "anonymous",
        "credential_label": None,
        "occurred_at_us": 1_700_000_000_000_001,
        "policy_revision": 7,
        "change_summary_json": {"changed_fields": ["/publication/status"], "reason": None},
    }
    values.update(overrides)
    return values


class Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class Connection:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.statements: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> Result:
        self.statements.append((sql, params))
        return Result(self._rows)


class Context:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __enter__(self) -> Connection:
        return self._connection

    def __exit__(self, *exc: object) -> None:
        return None


class Pool:
    def __init__(self, connection: Connection | None = None, *, error: BaseException | None = None):
        self._connection, self._error = connection or Connection(), error

    def connection(self, *, timeout: float | None = None) -> Context:
        if self._error is not None:
            raise self._error
        return Context(self._connection)


def reads(
    *, rows: list[dict[str, Any]] | None = None, timeout: float = 3
) -> tuple[PostgresAuditEventReads, Connection]:
    connection = Connection(rows)
    return PostgresAuditEventReads(cast(Any, Pool(connection)), timeout_seconds=timeout), connection


@pytest.mark.parametrize("timeout", [0, -1, 30.1, True])
def test_the_read_deadline_is_bounded(timeout: Any) -> None:
    with pytest.raises(ValueError, match="bounded deadlines"):
        PostgresAuditEventReads(cast(Any, Pool()), timeout_seconds=timeout)
    PostgresAuditEventReads(cast(Any, Pool()), timeout_seconds=0.5)
    PostgresAuditEventReads(cast(Any, Pool()), timeout_seconds=30)


def test_one_page_reads_read_only_with_the_composite_frontier_and_exact_action() -> None:
    reader, connection = reads(rows=[row()])
    query = AuditEventQuery(2, AuditEventPosition(*POSITION), "model_binding.retire")
    events = reader.scan(query)
    assert len(events) == 1
    assert events[0].event_id == "lifecycle-00000000000000000000000000000001"
    assert events[0].policy_revision == 7
    assert events[0].change_summary is not None
    assert events[0].change_summary.changed_fields == ("/publication/status",)
    sql = [statement for statement, _ in connection.statements]
    assert sql[0] == "SET TRANSACTION READ ONLY"
    assert sql[1].startswith("SELECT set_config('statement_timeout'")
    assert sql[2] == statement(query)[0]
    params = [parameters for _, parameters in connection.statements][2]
    assert params == (POSITION[0], POSITION[1], "model_binding.retire", 3)


def test_the_statement_carries_no_filter_when_none_was_requested() -> None:
    reader, connection = reads(rows=[])
    assert reader.scan(AuditEventQuery(1)) == ()
    _, parameters = connection.statements[2]
    assert parameters == (2,)


def test_a_row_of_another_kind_is_refused_rather_than_mislabeled() -> None:
    reader, _ = reads(rows=[row(kind="profile")])
    with pytest.raises(AuditEventUnavailableError):
        reader.scan(AuditEventQuery(1))


@pytest.mark.parametrize(
    "summary",
    [
        None,
        {"changed_fields": ["/publication/status", "/publication/retired_at"], "reason": "why"},
        {"changed_fields": ["/a"], "reason": None},
    ],
)
def test_a_summary_with_only_declared_keys_is_projected(summary: Any) -> None:
    reader, _ = reads(rows=[row(change_summary_json=summary)])
    events = reader.scan(AuditEventQuery(1))
    if summary is None:
        assert events[0].change_summary is None
    else:
        assert events[0].change_summary is not None
        assert list(events[0].change_summary.changed_fields) == summary["changed_fields"]
        assert events[0].change_summary.reason == summary["reason"]


@pytest.mark.parametrize(
    "summary",
    [
        {"changed_fields": ["/a"], "before": "private body"},
        {"changed_fields": "not-a-list"},
        {"changed_fields": []},
        {"changed_fields": ["status"]},
        {"changed_fields": ["/a"], "reason": "x" * 2001},
        ["/a"],
        7,
    ],
)
def test_an_untrusted_stored_summary_fails_closed(summary: Any) -> None:
    reader, _ = reads(rows=[row(change_summary_json=summary)])
    with pytest.raises(AuditEventUnavailableError):
        reader.scan(AuditEventQuery(1))


def test_a_row_missing_a_declared_column_fails_closed() -> None:
    incomplete = row()
    del incomplete["occurred_at_us"]
    reader, _ = reads(rows=[incomplete])
    with pytest.raises(AuditEventUnavailableError):
        reader.scan(AuditEventQuery(1))


def test_a_row_violating_the_domain_invariants_fails_closed() -> None:
    reader, _ = reads(rows=[row(policy_revision=0)])
    with pytest.raises(AuditEventUnavailableError):
        reader.scan(AuditEventQuery(1))


@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError("connection lost"),
        PoolTimeout("pool timeout"),
        PoolClosed("pool closed"),
        TooManyRequests("too many"),
    ],
)
def test_a_storage_failure_is_one_typed_unavailability(error: Exception) -> None:
    reader = PostgresAuditEventReads(cast(Any, Pool(error=error)))
    with pytest.raises(AuditEventUnavailableError):
        reader.scan(AuditEventQuery(1))


def test_an_expired_deadline_before_the_query_is_refused(monkeypatch: Any) -> None:
    moments = iter([0.0, 1.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(moments))
    reader, connection = reads(rows=[row()], timeout=0.5)
    with pytest.raises(AuditEventUnavailableError):
        reader.scan(AuditEventQuery(1))
    # The statement was never sent, so no partial row could be returned.
    assert connection.statements == []


def test_an_expired_deadline_after_the_query_discards_the_page(monkeypatch: Any) -> None:
    moments = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(moments))
    reader, _ = reads(rows=[row()], timeout=0.5)
    with pytest.raises(AuditEventUnavailableError):
        reader.scan(AuditEventQuery(1))


def test_the_statement_refuses_an_unvalidated_query() -> None:
    with pytest.raises(AuditEventUnavailableError):
        statement(cast(Any, object()))
