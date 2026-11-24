"""`insert_if_absent` 的三条判据:插入、已存在、其它异常照旧抛."""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_portable_sql.transaction import insert_if_absent, insert_if_absent_async


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


class _AsyncCursor:
    """异步形态的同一替身:动作相同,只是每个动作都要 `await`."""

    def __init__(self, *, fails: Exception | None = None, autocommit: bool = False) -> None:
        self.statements: list[str] = []
        self._fails = fails
        self.connection = type("_AsyncAutocommit", (), {"autocommit": autocommit})()

    async def execute(self, statement: str, params: Any = ()) -> None:
        self.statements.append(statement)
        if statement.startswith("INSERT") and self._fails is not None:
            raise self._fails


async def test_the_async_form_emits_exactly_the_same_statements_as_the_sync_form() -> None:
    """两条路径共用保存点名字与冲突判定,所以文本序列必须逐字相同."""

    sync_cursor = _Cursor()
    async_cursor = _AsyncCursor()

    assert insert_if_absent(sync_cursor, "INSERT INTO t (a) VALUES (?)", (1,)) is True
    assert await insert_if_absent_async(async_cursor, "INSERT INTO t (a) VALUES (?)", (1,)) is True
    assert async_cursor.statements == sync_cursor.statements


async def test_the_async_form_rolls_back_to_the_savepoint_on_a_duplicate() -> None:
    cursor = _AsyncCursor(fails=_UniqueViolationError("duplicate key"))

    assert await insert_if_absent_async(cursor, "INSERT INTO t (a) VALUES (?)", (1,)) is False
    assert cursor.statements[-2:] == [
        "ROLLBACK TO SAVEPOINT portable_sql_insert_if_absent",
        "RELEASE SAVEPOINT portable_sql_insert_if_absent",
    ]


async def test_the_async_form_does_not_translate_other_errors() -> None:
    cursor = _AsyncCursor(fails=_OtherError("serialization failure"))

    with pytest.raises(_OtherError):
        await insert_if_absent_async(cursor, "INSERT INTO t (a) VALUES (?)", (1,))
