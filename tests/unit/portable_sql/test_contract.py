"""契约与事务语义:参数风格、子集拒绝、提交未知、行形状与 CAS 判据。

这些用例跑在 fake DB-API 上,而 fake 用的是与真驱动同一份 `check_statement`,所以
「fake 能过」等于「契约能过」,不等于「某个品牌能过」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from speechnexus_portable_sql import (
    CommitUnknownError,
    ContractError,
    Row,
    SubsetViolationError,
    TransactionError,
    check_parameters,
    check_statement,
    keyset_page,
    transaction,
)
from tests.support.portable_sql_fake import FakeDatabase

SELECT_JOB = "SELECT job_id, job_state FROM coordination_jobs WHERE job_id = ?"
CAS_JOB = "UPDATE coordination_jobs SET job_state = ?, version = ? WHERE job_id = ? AND version = ?"


def job_row() -> Row:
    return Row(("job_id", "job_state"), ("job-1", "P"))


def test_qmark_statement_passes_and_returns_bound_parameters() -> None:
    assert check_statement(SELECT_JOB, ("job-1",)) == ("job-1",)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT job_id FROM coordination_jobs WHERE job_id = %s",
        "SELECT job_id FROM coordination_jobs WHERE job_id = $1",
        "SELECT job_id FROM coordination_jobs WHERE job_id = :job_id",
    ],
)
def test_non_qmark_parameter_styles_are_rejected(sql: str) -> None:
    with pytest.raises(SubsetViolationError):
        check_statement(sql, ("job-1",))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT job_id FROM coordination_jobs FOR UPDATE SKIP LOCKED",
        "INSERT INTO coordination_jobs (job_id) VALUES (?) RETURNING job_id",
        "INSERT INTO coordination_jobs (job_id) VALUES (?) ON CONFLICT DO NOTHING",
        "SELECT job_id FROM coordination_jobs LIMIT 10",
        "SELECT pg_advisory_lock(?)",
        "SELECT job_id FROM coordination_jobs WHERE due_at < now()",
        "SELECT payload::jsonb FROM coordination_jobs",
    ],
)
def test_forbidden_constructs_are_rejected(sql: str) -> None:
    with pytest.raises(SubsetViolationError):
        check_statement(sql, ("job-1",))


def test_parameter_count_must_match_placeholders() -> None:
    with pytest.raises(ContractError):
        check_statement(SELECT_JOB, ())
    with pytest.raises(ContractError):
        check_statement(SELECT_JOB, ("job-1", "extra"))


@pytest.mark.parametrize("value", [True, False, 1.5, b"bytes"])
def test_unportable_parameter_types_are_rejected(value: object) -> None:
    with pytest.raises(ContractError):
        check_parameters((value,))


def test_decimal_and_utc_datetime_are_accepted_and_offset_is_rejected() -> None:
    assert check_parameters((Decimal("12"), datetime(2026, 11, 21, tzinfo=UTC))) == (
        Decimal("12"),
        datetime(2026, 11, 21, tzinfo=UTC),
    )
    with pytest.raises(ContractError):
        check_parameters((datetime(2026, 11, 21, tzinfo=timezone(timedelta(hours=8))),))


def test_transaction_commits_once_on_success() -> None:
    database = FakeDatabase({SELECT_JOB: [[job_row()]]})
    connection = database.connect()
    with transaction(connection) as cursor:
        cursor.execute(SELECT_JOB, ("job-1",))
        assert cursor.fetchall() == [job_row()]
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_transaction_rolls_back_and_reports_failure() -> None:
    database = FakeDatabase({}, fail_execute=RuntimeError("driver exploded"))
    connection = database.connect()
    with pytest.raises(TransactionError), transaction(connection):
        connection.cursor().execute(CAS_JOB, ("S", 2, "job-1", 1))
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_commit_failure_is_commit_unknown_and_never_retried() -> None:
    database = FakeDatabase({SELECT_JOB: [[job_row()]]}, fail_commit=True)
    connection = database.connect()
    with pytest.raises(CommitUnknownError), transaction(connection) as cursor:
        cursor.execute(SELECT_JOB, ("job-1",))
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_rowcount_is_the_only_cas_verdict() -> None:
    database = FakeDatabase({}, rowcounts={CAS_JOB: 0})
    connection = database.connect()
    with transaction(connection) as cursor:
        cursor.execute(CAS_JOB, ("S", 2, "job-1", 1))
        assert cursor.rowcount == 0


def test_row_supports_position_and_name_access() -> None:
    row = job_row()
    assert row["job_id"] == row.at(0) == row[0] == "job-1"
    assert row["job_state"] == "P"
    assert row.keys() == ("job_id", "job_state")
    assert row.as_dict() == {"job_id": "job-1", "job_state": "P"}
    assert "job_id" in row
    with pytest.raises(KeyError):
        row["missing"]


def test_keyset_page_reports_has_more_without_limit() -> None:
    rows = [job_row() for _ in range(3)]
    page, has_more = keyset_page(rows, 2)
    assert len(page) == 2
    assert has_more is True
    assert keyset_page(rows[:2], 2) == (rows[:2], False)
    with pytest.raises(ContractError):
        keyset_page(rows, 0)
    with pytest.raises(ContractError):
        keyset_page(rows, 1)
