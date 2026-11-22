"""显式事务:提交成功才算提交,执行失败确定回滚,提交异常只报 `commit_unknown`。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from speechnexus_portable_sql.contract import (
    CommitUnknownError,
    ConflictError,
    Connection,
    Cursor,
    Params,
    TransactionError,
)

__all__ = ["insert_if_absent", "savepoint", "transaction"]


@contextmanager
def transaction(connection: Connection) -> Iterator[Cursor]:
    """打开游标,成功时提交,失败时回滚.

    失败语义:

    - `yield` 内的异常:回滚后以 `TransactionError` 抛出,调用方可确定「没有提交」。
    - `commit()` 抛错:结果不可确认,抛 `CommitUnknownError`。调用方只能按原业务身份查询,
      不得新建执行、不得提前释放预算。
    """

    cursor = connection.cursor()
    try:
        try:
            yield cursor
        except BaseException as error:
            _rollback_quietly(connection)
            raise TransactionError("transaction failed and was rolled back") from error
        else:
            try:
                connection.commit()
            except BaseException as error:
                _rollback_quietly(connection)
                raise CommitUnknownError("commit outcome unknown") from error
    finally:
        cursor.close()


def _rollback_quietly(connection: Connection) -> None:
    # 回滚失败不改变「未提交」这一结论,只丢连接。
    with suppress(Exception):
        connection.rollback()


#: 保存点名字只允许普通标识符:S QL 不允许把标识符参数化,放行前先按字符集收窄。
_SAVEPOINT_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


@contextmanager
def savepoint(cursor: Any, name: str = "portable_sql_sp") -> Iterator[None]:
    """在一个保存点内执行语句,失败时回滚到保存点而不是放弃整个事务.

    为什么需要它:受限方言没有 `ON CONFLICT`。用 `INSERT ... WHERE NOT EXISTS` 顶替时,
    竞争下较晚的写者会撞唯一索引,PG 会把**整个事务**标记为中止 —— 在 Python 里捕获异常
    并不会让事务恢复,后续语句全部失败。真正常用的写法只有一种:先把这一步包进保存点,
    冲突后 `ROLLBACK TO SAVEPOINT`,事务回到可用状态。

    `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` / `RELEASE SAVEPOINT` 是标准 SQL,PostgreSQL 与
    MySQL/InnoDB 都支持,所以这层不引入品牌分支。
    """

    if not _SAVEPOINT_NAME.match(name):
        raise ValueError("a savepoint name must be a plain SQL identifier")
    cursor.execute("SAVEPOINT " + name)
    try:
        yield
    except BaseException:
        # 回滚到保存点后还要释放,否则保存点会在事务里越积越多。
        cursor.execute("ROLLBACK TO SAVEPOINT " + name)
        cursor.execute("RELEASE SAVEPOINT " + name)
        raise
    else:
        cursor.execute("RELEASE SAVEPOINT " + name)


def insert_if_absent(cursor: Any, statement: str, params: Params = ()) -> bool:
    """在保存点内执行一条自带 `WHERE NOT EXISTS` 的 INSERT,返回是否真的插入了一行.

    受限方言没有 `ON CONFLICT`/`MERGE`。等价写法是
    `INSERT ... SELECT ... WHERE NOT EXISTS (SELECT 1 FROM ... WHERE key = ?)`;
    两个写者同时通过 NOT EXISTS 检查时,较晚的那个会撞唯一索引,驱动把它翻成
    `ConflictError` —— 这正是「别人先插了」,语义与 `ON CONFLICT DO NOTHING` 相同。

    **保存点是必要条件,不是优化**:PG 里唯一冲突会中止整个事务,捕获 Python 异常不会让事务
    恢复;`ROLLBACK TO SAVEPOINT` 才把它拉回可用状态。没有保存点的版本会在冲突之后让后续语句
    全部失败 —— 这条已被真实 PG oracle 推翻过一次,见计划 §8.19。

    判据是 `rowcount`,不是异常类型。
    """

    inserted = False
    try:
        with savepoint(cursor):
            cursor.execute(statement, params)
            rowcount: int = cursor.rowcount
            inserted = rowcount == 1
    except ConflictError:
        return False
    return inserted
