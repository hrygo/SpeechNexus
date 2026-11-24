"""键锁原语:先插后锁的顺序、冲突仍能锁、自动提交拒绝、桶号确定性与碰撞."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest

from speechnexus_portable_sql.contract import ContractError, Cursor
from speechnexus_portable_sql.keyed_lock import (
    KEYED_LOCK_BUCKETS,
    KEYED_LOCK_TABLE,
    KeyedLock,
    acquire,
    bucket_for_key,
    key_digest,
    lock_statements,
)

SAVEPOINT = "portable_sql_insert_if_absent"


class _UniqueViolationError(Exception):
    sqlstate = "23505"


class _OtherError(Exception):
    sqlstate = "40001"


class _Cursor:
    def __init__(self, *, rows: Sequence[object] = (), fails: Exception | None = None) -> None:
        self.statements: list[str] = []
        self.params: list[tuple[object, ...]] = []
        self._rows = list(rows)
        self._fails = fails
        self.closed = False

    def execute(self, statement: str, params: Any = ()) -> None:
        self.statements.append(statement)
        self.params.append(tuple(params))
        if statement.startswith("INSERT") and self._fails is not None:
            raise self._fails

    def fetchall(self) -> list[object]:
        rows, self._rows = self._rows, []
        return rows

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, cursor: _Cursor, *, autocommit: bool = False) -> None:
        self._cursor = cursor
        self.autocommit = autocommit
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> Cursor:
        return cast(Cursor, self._cursor)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _bucket(key: str = "workbench:project.create:workspace:p:key") -> int:
    return int(bucket_for_key(key))


def test_the_insert_comes_first_and_the_row_lock_comes_second() -> None:
    cursor = _Cursor(rows=[(int(bucket_for_key("k")),)])
    result = acquire(_Connection(cursor), key="k")

    assert result == KeyedLock(bucket_no=bucket_for_key("k"), digest=key_digest("k"), created=True)
    assert cursor.statements == [
        f"SAVEPOINT {SAVEPOINT}",
        f"INSERT INTO {KEYED_LOCK_TABLE} (bucket_no) VALUES (?)",
        f"RELEASE SAVEPOINT {SAVEPOINT}",
        f"SELECT bucket_no FROM {KEYED_LOCK_TABLE} WHERE bucket_no = ? FOR UPDATE",
    ]
    # 两条语句绑定同一个桶号:先等行存在,再锁同一行。
    assert cursor.params[1] == (bucket_for_key("k"),)
    assert cursor.params[3] == (bucket_for_key("k"),)


def test_an_existing_row_rolls_back_to_the_savepoint_and_still_locks() -> None:
    """唯一冲突是正常路径:它是「行已经在了」的证据,不是错误。"""

    cursor = _Cursor(rows=[(int(bucket_for_key("k")),)], fails=_UniqueViolationError("duplicate"))
    result = acquire(_Connection(cursor), key="k")

    assert result.created is False
    assert cursor.statements == [
        f"SAVEPOINT {SAVEPOINT}",
        f"INSERT INTO {KEYED_LOCK_TABLE} (bucket_no) VALUES (?)",
        f"ROLLBACK TO SAVEPOINT {SAVEPOINT}",
        f"RELEASE SAVEPOINT {SAVEPOINT}",
        f"SELECT bucket_no FROM {KEYED_LOCK_TABLE} WHERE bucket_no = ? FOR UPDATE",
    ]


def test_other_errors_are_not_translated_into_a_missing_row() -> None:
    cursor = _Cursor(fails=_OtherError("serialization failure"))

    with pytest.raises(_OtherError):
        acquire(_Connection(cursor), key="k")


def test_a_row_that_vanishes_between_insert_and_lock_is_a_contract_error() -> None:
    cursor = _Cursor(rows=())

    with pytest.raises(ContractError):
        acquire(_Connection(cursor), key="k")


def test_a_schema_qualifies_both_statements_from_the_same_template() -> None:
    insert, lock = lock_statements("wb_synthetic")

    assert insert == f"INSERT INTO wb_synthetic.{KEYED_LOCK_TABLE} (bucket_no) VALUES (?)"
    assert lock == (
        f"SELECT bucket_no FROM wb_synthetic.{KEYED_LOCK_TABLE} WHERE bucket_no = ? FOR UPDATE"
    )
    assert lock_statements() == (
        f"INSERT INTO {KEYED_LOCK_TABLE} (bucket_no) VALUES (?)",
        f"SELECT bucket_no FROM {KEYED_LOCK_TABLE} WHERE bucket_no = ? FOR UPDATE",
    )


@pytest.mark.parametrize("schema", ["Workbench", "wb-synthetic", "", "pg_temp", "public"])
def test_a_schema_that_is_not_a_plain_identifier_is_refused_before_any_statement(
    schema: str,
) -> None:
    cursor = _Cursor(rows=[(0,)])

    with pytest.raises(ContractError):
        acquire(_Connection(cursor), key="k", schema=schema)

    assert cursor.statements == []


def test_an_autocommit_connection_is_refused_instead_of_silently_not_locking() -> None:
    cursor = _Cursor(rows=[(0,)])

    with pytest.raises(ContractError):
        acquire(_Connection(cursor, autocommit=True), key="k")

    assert cursor.statements == []


def test_the_same_key_always_lands_on_the_same_bucket_and_the_digest_is_evidence() -> None:
    assert bucket_for_key("a:b") == bucket_for_key("a:b")
    assert bucket_for_key("a:b") != bucket_for_key("a:b ")  # 空白是键的一部分
    assert key_digest("a:b").startswith("sha256:")
    assert len(key_digest("a:b")) == len("sha256:") + 64
    assert 0 <= int(bucket_for_key("a:b")) < KEYED_LOCK_BUCKETS
    assert bucket_for_key("a:b") == bucket_for_key("a:b", buckets=KEYED_LOCK_BUCKETS)


def test_two_different_keys_can_share_one_bucket_row() -> None:
    """碰撞是设计内的:桶数远小于键空间,同桶只意味着两笔事务互等。"""

    seen: dict[int, str] = {}
    collision: tuple[str, str] | None = None
    for index in range(10_000):
        key = f"collision-probe-{index}"
        bucket = int(bucket_for_key(key))
        if bucket in seen:
            collision = (seen[bucket], key)
            break
        seen[bucket] = key

    assert collision is not None
    first, second = collision
    assert first != second
    assert bucket_for_key(first) == bucket_for_key(second)
    assert key_digest(first) != key_digest(second)


def test_a_bucket_count_below_one_is_refused() -> None:
    with pytest.raises(ContractError):
        bucket_for_key("k", buckets=0)


def test_an_empty_key_is_refused() -> None:
    with pytest.raises(ContractError):
        bucket_for_key("")
