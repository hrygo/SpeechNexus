"""Capacity settlement resolves the original lease and replays only unknown outcomes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

import psycopg
import pytest

from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.job_dispatch import DispatchUnavailableError
from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import PostgresLeaseSettler
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    require_complete_tts_roots,
)

OPERATION = "job-" + "1" * 32
LEASE = "lease-" + "2" * 32


@dataclass(frozen=True)
class _Fact:
    lease_state: str
    version: int = 1


@dataclass(frozen=True)
class _Result:
    fact: _Fact | None
    outcome: str = "committed"


class _Leases:
    def __init__(
        self,
        *,
        current: _Fact | None = None,
        results: list[_Result] | None = None,
    ) -> None:
        self.current = current
        self.results = list(results or [])
        self.queries: list[str] = []
        self.released: list[ReleaseLease] = []

    def query(self, lease_id: str) -> _Result:
        self.queries.append(lease_id)
        return _Result(self.current)

    def release(self, request: ReleaseLease) -> _Result:
        self.released.append(request)
        return self.results.pop(0) if self.results else _Result(None, "rejected")


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[dict[str, Any]], *, fail: bool) -> None:
        self._rows, self._fail = rows, fail
        self.statements: list[str] = []

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> _Rows:
        if self._fail:
            raise psycopg.OperationalError("storage unavailable")
        self.statements.append(statement)
        if "FROM admission_leases" in statement:
            return _Rows(self._rows)
        return _Rows([])


class _Pool:
    def __init__(self, rows: list[dict[str, Any]], *, fail: bool = False) -> None:
        self._rows, self._fail = rows, fail

    def connection(self, *, timeout: float) -> _Connection:
        return _Connection(self._rows, fail=self._fail)


def _settler(
    rows: list[dict[str, Any]], leases: _Leases, *, fail: bool = False
) -> PostgresLeaseSettler:
    settler = PostgresLeaseSettler(cast(SpeechPool, _Pool(rows, fail=fail)), timeout_seconds=1)
    settler._leases = cast(LeaseService, leases)
    return settler


def _release(rows: list[dict[str, Any]], leases: _Leases, *, fail: bool = False) -> bool:
    return asyncio.run(_settler(rows, leases, fail=fail).release(OPERATION))


def test_a_job_without_a_lease_is_already_settled() -> None:
    leases = _Leases()

    assert _release([], leases) is True
    assert leases.released == []


def test_a_multi_stage_admission_is_left_to_reconciliation() -> None:
    rows = [{"lease_id": LEASE}, {"lease_id": "lease-" + "3" * 32}]
    leases = _Leases()

    assert _release(rows, leases) is False
    assert leases.released == []


def test_an_already_released_lease_is_idempotent() -> None:
    leases = _Leases(current=_Fact("released"))

    assert _release([{"lease_id": LEASE}], leases) is True
    assert leases.released == []


def test_a_committed_release_returns_true() -> None:
    leases = _Leases(current=_Fact("reserved"), results=[_Result(_Fact("released"))])

    assert _release([{"lease_id": LEASE}], leases) is True
    assert leases.released == [ReleaseLease(LEASE, 1)]


def test_a_refused_release_leaves_the_lease_for_reconciliation() -> None:
    leases = _Leases(current=_Fact("executing"), results=[_Result(None, "rejected")])

    assert _release([{"lease_id": LEASE}], leases) is False
    assert len(leases.released) == 1


def test_an_unknown_release_is_retried_once_then_left() -> None:
    leases = _Leases(
        current=_Fact("executing"),
        results=[_Result(None, "unknown"), _Result(None, "unknown")],
    )

    assert _release([{"lease_id": LEASE}], leases) is False
    assert len(leases.released) == 2
    assert len(leases.queries) == 2


def test_an_unknown_release_that_then_commits_returns_true() -> None:
    leases = _Leases(
        current=_Fact("executing"),
        results=[_Result(None, "unknown"), _Result(_Fact("released"))],
    )

    assert _release([{"lease_id": LEASE}], leases) is True
    assert len(leases.released) == 2


def test_a_missing_lease_fact_does_not_release() -> None:
    leases = _Leases(current=None)

    assert _release([{"lease_id": LEASE}], leases) is False
    assert leases.released == []


def test_unreadable_storage_is_unavailable_not_released() -> None:
    with pytest.raises(DispatchUnavailableError):
        _release([{"lease_id": LEASE}], _Leases(), fail=True)


def test_the_settler_requires_a_bounded_timeout() -> None:
    with pytest.raises(ValueError):
        PostgresLeaseSettler(cast(SpeechPool, _Pool([])), timeout_seconds=0)


def test_the_settler_rejects_an_invalid_operation_id() -> None:
    with pytest.raises(ValueError):
        asyncio.run(_settler([], _Leases()).release("not a valid id"))


class _PreflightResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _PreflightConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.statements: list[str] = []

    def __enter__(self) -> _PreflightConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> _PreflightResult:
        self.statements.append(statement)
        return _PreflightResult(self._row if "FROM admission_leases" in statement else None)


class _PreflightPool:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.connections: list[_PreflightConnection] = []

    def connection(self, *, timeout: float) -> _PreflightConnection:
        connection = _PreflightConnection(self.row)
        self.connections.append(connection)
        return connection


def test_preflight_passes_when_every_lease_is_settled() -> None:
    pool = _PreflightPool(None)

    require_complete_tts_roots(cast(SpeechPool, pool), timeout_seconds=1)

    statements = pool.connections[0].statements
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert "l.operation_id LIKE 'job-%'" in statements[2]


def test_preflight_refuses_a_public_lease_on_its_identity() -> None:
    pool = _PreflightPool({"operation_id": OPERATION, "attempt_id": "attempt-1", "lease_id": LEASE})

    with pytest.raises(ValueError, match="reconcile first"):
        require_complete_tts_roots(cast(SpeechPool, pool))


def test_preflight_requires_a_bounded_timeout() -> None:
    with pytest.raises(ValueError):
        require_complete_tts_roots(cast(SpeechPool, _PreflightPool(None)), timeout_seconds=0)
