"""`insert_if_absent` 的三条判据:插入、已存在、其它异常照旧抛."""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_portable_sql.transaction import insert_if_absent


class _UniqueViolationError(Exception):
    sqlstate = "23505"


class _OtherError(Exception):
    sqlstate = "40001"


class _Cursor:
    def __init__(self, *, fails: Exception | None = None) -> None:
        self.statements: list[str] = []
        self._fails = fails

    def execute(self, statement: str, params: Any = ()) -> None:
        self.statements.append(statement)
        if statement.startswith("INSERT") and self._fails is not None:
            raise self._fails


def test_inserts_and_reports_true() -> None:
    cursor = _Cursor()
    assert insert_if_absent(cursor, "INSERT INTO t (a) VALUES (?)", (1,)) is True
    assert cursor.statements == [
        "SAVEPOINT portable_sql_insert_if_absent",
        "INSERT INTO t (a) VALUES (?)",
        "RELEASE SAVEPOINT portable_sql_insert_if_absent",
    ]


def test_unique_violation_rolls_back_to_the_savepoint_and_reports_false() -> None:
    cursor = _Cursor(fails=_UniqueViolationError("duplicate key"))
    assert insert_if_absent(cursor, "INSERT INTO t (a) VALUES (?)", (1,)) is False
    # 冲突后必须回滚到保存点并释放,否则事务里的保存点会越积越多、事务也仍是中止状态。
    assert cursor.statements[-2:] == [
        "ROLLBACK TO SAVEPOINT portable_sql_insert_if_absent",
        "RELEASE SAVEPOINT portable_sql_insert_if_absent",
    ]


def test_other_errors_are_not_translated() -> None:
    cursor = _Cursor(fails=_OtherError("serialization failure"))
    with pytest.raises(_OtherError):
        insert_if_absent(cursor, "INSERT INTO t (a) VALUES (?)", (1,))


class _AutocommitConnection:
    autocommit = True


class _AutocommitCursor(_Cursor):
    def __init__(self, *, fails: Exception | None = None) -> None:
        super().__init__(fails=fails)
        self.connection = _AutocommitConnection()


def test_autocommit_connection_skips_the_savepoint() -> None:
    """自动提交连接没有外层事务,开保存点会被 PG 直接拒绝(25P01)."""

    cursor = _AutocommitCursor()
    assert insert_if_absent(cursor, "INSERT INTO t (a) VALUES (?)", (1,)) is True
    assert cursor.statements == ["INSERT INTO t (a) VALUES (?)"]


def test_autocommit_connection_still_reports_a_duplicate_as_no_insert() -> None:
    cursor = _AutocommitCursor(fails=_UniqueViolationError())

    assert insert_if_absent(cursor, "INSERT INTO t (a) VALUES (?)", (1,)) is False
    assert cursor.statements == ["INSERT INTO t (a) VALUES (?)"]
