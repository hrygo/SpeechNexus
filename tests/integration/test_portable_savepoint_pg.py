"""保存点原语的 oracle:另一个事务先提交同一键之后,当前事务必须仍然可用.

同一个事务里连插两次不会冲突(NOT EXISTS 看得见自己那一行),所以这条用例必须用**两个连接**
造出真实竞争:连接 A 先插同键并保持事务打开,连接 B 的插入会在唯一索引上等待,A 提交后 B 撞
唯一冲突 —— 这正是 `ON CONFLICT DO NOTHING` 之外唯一可用的路径,也是保存点存在的理由。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from speechnexus_portable_sql.contract import ConflictError
from speechnexus_portable_sql.psycopg_driver import connect
from speechnexus_portable_sql.transaction import insert_if_absent, savepoint

pytestmark = [pytest.mark.integration]

_TABLE = "portable_savepoint_oracle"
_INSERT = (
    f"INSERT INTO {_TABLE} (key_digest, payload) SELECT ?, ? "
    f"WHERE NOT EXISTS (SELECT 1 FROM {_TABLE} WHERE key_digest = ?)"
)


def _dsn() -> str:
    value = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL", "")
    if not value:
        pytest.fail(
            "Database integration cases must fail instead of skipping: "
            "SPEECHNEXUS_TEST_DATABASE_URL is not set"
        )
    return value


@pytest.fixture
def schema() -> Iterator[str]:
    dsn = _dsn()
    with connect(dsn, row_factory=dict_row) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        conn.execute(
            f"CREATE TABLE {_TABLE} (key_digest VARCHAR(64) PRIMARY KEY, payload INTEGER NOT NULL)"
        )
        conn.commit()
    yield dsn
    with connect(dsn, row_factory=dict_row) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        conn.commit()


def _hold_and_commit(dsn: str, ready: threading.Event) -> None:
    """另一个会话先插同键,等 B 撞上之后再提交 —— 冲突此刻才成立。"""

    with connect(dsn, row_factory=dict_row) as holder:
        holder.execute(f"INSERT INTO {_TABLE} (key_digest, payload) VALUES (?, ?)", ("k", 1))
        ready.set()
        time.sleep(0.5)
        holder.commit()


def test_a_cross_session_conflict_is_absorbed_and_the_transaction_stays_usable(
    schema: str,
) -> None:
    ready = threading.Event()
    holder = threading.Thread(target=_hold_and_commit, args=(schema, ready), daemon=True)
    with connect(schema, row_factory=dict_row) as conn:
        holder.start()
        assert ready.wait(5.0)
        cursor = conn.cursor()
        # 这一行在唯一索引上等待,holder 提交后变成唯一冲突。
        assert insert_if_absent(cursor, _INSERT, ("k", 2, "k")) is False
        # 关键断言:事务没有被中止,后面的语句照常执行,而且看到的是 holder 那一行。
        cursor.execute(f"SELECT payload FROM {_TABLE} WHERE key_digest = ?", ("k",))
        assert cursor.fetchone() == {"payload": 1}
    holder.join(5.0)


def test_without_a_savepoint_the_same_conflict_poisons_the_transaction(schema: str) -> None:
    """注入即红:去掉保存点,同样的冲突会让后续语句失败。"""

    ready = threading.Event()
    holder = threading.Thread(target=_hold_and_commit, args=(schema, ready), daemon=True)
    with connect(schema, row_factory=dict_row) as conn:
        holder.start()
        assert ready.wait(5.0)
        cursor = conn.cursor()
        with pytest.raises(ConflictError):
            cursor.execute(_INSERT, ("k", 2, "k"))
        # 事务被中止:后续语句不再是「查不到」,而是直接失败(25P02)。
        with pytest.raises(psycopg.errors.InFailedSqlTransaction):
            cursor.execute(f"SELECT payload FROM {_TABLE} WHERE key_digest = ?", ("k",))
    holder.join(5.0)


def test_a_failing_statement_inside_a_savepoint_does_not_poison_the_transaction(
    schema: str,
) -> None:
    with connect(schema, row_factory=dict_row) as conn:
        cursor = conn.cursor()
        cursor.execute(f"INSERT INTO {_TABLE} (key_digest, payload) VALUES (?, ?)", ("a", 1))
        with pytest.raises(ConflictError), savepoint(cursor):
            cursor.execute(f"INSERT INTO {_TABLE} (key_digest, payload) VALUES (?, ?)", ("a", 2))
        cursor.execute(f"SELECT COUNT(*) AS total FROM {_TABLE}")
        assert cursor.fetchone() == {"total": 1}
