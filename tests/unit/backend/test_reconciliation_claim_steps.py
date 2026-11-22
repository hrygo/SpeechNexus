"""抢占账本的两个分支:插入新行,或按读到的次数做 CAS 更新."""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_backend.speech.infrastructure import (
    job_cancellation_reconciliation_postgres as ledger,
)

NOW = 1_700_000_000_000_000
LEASE = NOW + 5_000_000


class DuplicateError(Exception):
    sqlstate = "23505"


class Cursor:
    """按脚本回答查询;插入的冲突由 ``duplicate_insert`` 表达."""

    def __init__(
        self,
        *,
        stored: list[dict[str, Any]],
        after: list[dict[str, Any]] | None = None,
        update_rowcount: int = 1,
        duplicate_insert: bool = False,
    ) -> None:
        self._stored = list(stored)
        self._after = list(after or [])
        self.update_rowcount = update_rowcount
        self.duplicate_insert = duplicate_insert
        self.rowcount = 0
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: object = ()) -> Cursor:
        statement = query.lstrip()
        self.statements.append((query, tuple(parameters) if isinstance(parameters, tuple) else ()))
        if statement.startswith(("SAVEPOINT ", "ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT")):
            self.rowcount = 0
            return self
        if statement.startswith("INSERT INTO") and self.duplicate_insert:
            raise DuplicateError(query)
        if statement.startswith(("INSERT INTO", "UPDATE")):
            self.rowcount = self.update_rowcount
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        if not self._stored and self._after:
            return self._after
        stored, self._stored = self._stored, []
        return stored

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        return self.fetchall()[:size]


def record(attempts: int = 2, **changes: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "attempts": attempts,
        "settled_at_us": None,
        "last_outcome": "retry",
        "next_check_at_us": NOW - 1,
    }
    row.update(changes)
    return row


def test_a_missing_ledger_row_is_claimed_by_one_insert() -> None:
    cursor = Cursor(stored=[])
    assert ledger._take(cursor, "job-1", now_us=NOW, lease_until_us=LEASE) == 1
    inserted = [
        query.lstrip().split()[0] for query, _ in cursor.statements if "INSERT INTO" in query
    ]
    assert inserted == ["INSERT"]


def test_a_due_row_is_claimed_by_a_cas_update_carrying_the_read_count() -> None:
    cursor = Cursor(stored=[record(attempts=2)])
    assert ledger._take(cursor, "job-1", now_us=NOW, lease_until_us=LEASE) == 3
    update, parameters = next(
        (query, values)
        for query, values in cursor.statements
        if query.lstrip().startswith("UPDATE")
    )
    assert "attempts = ?" in update and "AND attempts = ?" in update
    assert parameters[0] == 3 and parameters[4] == 2


def test_a_spent_or_settled_row_is_not_claimed() -> None:
    settled = Cursor(stored=[record(settled_at_us=NOW)])
    assert ledger._take(settled, "job-1", now_us=NOW, lease_until_us=LEASE) is None
    exhausted = Cursor(stored=[record(last_outcome="exhausted")])
    assert ledger._take(exhausted, "job-1", now_us=NOW, lease_until_us=LEASE) is None
    future = Cursor(stored=[record(next_check_at_us=NOW + 1)])
    assert ledger._take(future, "job-1", now_us=NOW, lease_until_us=LEASE) is None


def test_saturated_attempts_stay_claimable_at_the_ceiling() -> None:
    cursor = Cursor(stored=[record(attempts=ledger._MAX_ATTEMPTS)])
    assert ledger._take(cursor, "job-1", now_us=NOW, lease_until_us=LEASE) == ledger._MAX_ATTEMPTS


def test_a_lost_cas_does_not_claim_the_row() -> None:
    cursor = Cursor(stored=[record()], update_rowcount=0)
    assert ledger._take(cursor, "job-1", now_us=NOW, lease_until_us=LEASE) is None


def test_an_insert_that_loses_the_race_falls_back_to_the_cas_path() -> None:
    cursor = Cursor(stored=[], after=[record(attempts=1)], duplicate_insert=True)
    assert ledger._take(cursor, "job-1", now_us=NOW, lease_until_us=LEASE) == 2
    assert any(query.lstrip().startswith("UPDATE") for query, _ in cursor.statements)


@pytest.mark.parametrize("attempts", [0, 1, 63])
def test_every_read_count_is_clamped_forward(attempts: int) -> None:
    cursor = Cursor(stored=[record(attempts=attempts)])
    assert ledger._take(cursor, "job-1", now_us=NOW, lease_until_us=LEASE) == attempts + 1
