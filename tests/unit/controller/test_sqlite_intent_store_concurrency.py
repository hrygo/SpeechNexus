"""真实 SQLite 上的并发 oracle:`schema_version` 是真 upsert,不是「插入即读」.

两条判据各自独立:

- 同一个键被多个写者反复改写时,最后一次提交的值必须等于所有写者写入的结果之和 ——
  丢失更新会让这个和变小,而把 upsert 折成「插入即读」会让它停在第一版;
- `UPDATE` 与 `INSERT` 之间被对面抢先插入时,冲突只回滚那一步,事务还能接着把值写进去。

写者之间用 `threading.Barrier` 同时起跑,各自持有独立连接,并用存储自己的 `_Transaction`
(`BEGIN IMMEDIATE`)开事务。`BEGIN IMMEDIATE` 的库级写锁把写者串行化,所以每个写者在锁内
读到的当前值必须互不相同(0, 1, …, N*M-1)。这条「观察到的值是一个无重复的连续序列」是
交错发生的直接证据,不是顺序调用的累计。

不在本文件里的判据:同时**首次打开**同一个存储文件。`SqliteIntentStore.__init__` 在设置
`busy_timeout` 之前先跑 `PRAGMA journal_mode=WAL`,所以那一步在并发下会以 `database is
locked` 失败。这不是本次改动的语义(存储是节点本地状态,一个节点一个控制器进程,进程内由
`self._lock` 串行化),所以不在这里断言。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import cast

from speechnexus_node_controller.infrastructure.sqlite_intent_store import (
    SqliteIntentStore,
    _Transaction,
    _upsert_meta,
)

#: 每个写者循环的次数。总增量 = WRITERS * INCREMENTS,足以让多个写者反复排队。
WRITERS = 4
INCREMENTS = 25


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _store_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    SqliteIntentStore(path).close()
    return path


def test_concurrent_meta_writes_converge_without_a_lost_update(tmp_path: Path) -> None:
    path = _store_file(tmp_path, "concurrent-upsert.sqlite3")

    barrier = threading.Barrier(WRITERS)
    observed: list[tuple[str, int]] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def count_up(name: str) -> None:
        connection = _connect(path)
        try:
            barrier.wait(timeout=10)
            for _ in range(INCREMENTS):
                with _Transaction(connection):
                    row = connection.execute(
                        "SELECT value FROM runtime_intent_store_meta WHERE key = 'counter'"
                    ).fetchone()
                    current = int(row[0]) if row is not None else 0
                    _upsert_meta(connection, "counter", str(current + 1))
                with guard:
                    observed.append((name, current))
        except BaseException as error:
            with guard:
                errors.append(error)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=count_up, args=(f"writer-{index}",), daemon=True)
        for index in range(WRITERS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not any(thread.is_alive() for thread in threads), "a writer deadlocked"
    assert errors == [], [repr(error) for error in errors]
    total = WRITERS * INCREMENTS
    # 每个增量都在写锁内读当时的值:互不重复且覆盖 0..total-1,才说明没有两次增量
    # 看到同一个旧值(丢失更新),也没有跳过一个值。
    assert sorted(value for _, value in observed) == list(range(total))
    connection = sqlite3.connect(path)
    try:
        stored = connection.execute(
            "SELECT value FROM runtime_intent_store_meta WHERE key = 'counter'"
        ).fetchone()
    finally:
        connection.close()
    assert stored is not None and int(stored[0]) == total


class _ConflictAfterTheUpdate:
    """把「对面在 `UPDATE` 与 `INSERT` 之间插入同一个键」做成确定性的交错.

    SQLite 的写锁让真实并发几乎不可能正好落在这两步之间,所以这里只让**第一条** `UPDATE`
    的 `rowcount` 报 0(那一行其实已经由另一个连接写好),其余语句原样转发到底座连接 ——
    `INSERT` 因此撞真实唯一键,冲突路径必定被走到。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._cursor = connection.cursor()
        self._lie = True

    def cursor(self) -> _ConflictAfterTheUpdate:
        return self

    @property
    def rowcount(self) -> int:
        if self._lie:
            self._lie = False
            return 0
        return self._cursor.rowcount

    def execute(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> _ConflictAfterTheUpdate:
        # 必须返回自己,否则 `cursor.execute(...).rowcount` 读到的是底座连接的真实行数,
        # 这一跳就白撒了。
        self._cursor.execute(statement, parameters)
        return self


def test_the_savepoint_absorbs_a_conflict_landing_between_the_two_steps(tmp_path: Path) -> None:
    path = _store_file(tmp_path, "conflict.sqlite3")

    connection = _connect(path)
    try:
        with _Transaction(connection):
            _upsert_meta(
                cast(sqlite3.Connection, _ConflictAfterTheUpdate(connection)),
                "schema_version",
                "7",
            )
    finally:
        connection.close()

    connection = sqlite3.connect(path)
    try:
        stored = connection.execute(
            "SELECT value FROM runtime_intent_store_meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        connection.close()
    assert stored == ("7",)
