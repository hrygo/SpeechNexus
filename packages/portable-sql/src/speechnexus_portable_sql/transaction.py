"""显式事务:提交成功才算提交,执行失败确定回滚,提交异常只报 `commit_unknown`。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import Any

from speechnexus_portable_sql.contract import (
    CommitUnknownError,
    Connection,
    Cursor,
    TransactionError,
)

__all__ = [
    "SAVEPOINT_CONTROL",
    "in_transaction",
    "insert_if_absent",
    "insert_if_absent_async",
    "is_savepoint_control",
    "savepoint",
    "savepoint_async",
    "transaction",
]


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


#: 保存点控制语句的前缀。事务被中止之后,唯一还能执行的就是它们;任何「先探测再执行」
#: 的适配器(例如先装 `statement_timeout`)都必须先看这里,否则探针自己会先失败。
SAVEPOINT_CONTROL = ("SAVEPOINT ", "ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT")


def is_savepoint_control(statement: str) -> bool:
    """判断语句是不是保存点控制语句(唯一一条定义)."""

    return statement.startswith(SAVEPOINT_CONTROL)


#: 保存点名字只允许普通标识符:S QL 不允许把标识符参数化,放行前先按字符集收窄。
_SAVEPOINT_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _savepoint_control(name: str) -> tuple[str, str, str]:
    """保存点控制语句的唯一拼写点:`(开始, 回滚到, 释放)`.

    同步与异步两份上下文管理器共用这一段,所以两个形态发出的文本不可能分叉 —— 名字校验、
    关键字顺序与空格都只写一次。
    """

    if not _SAVEPOINT_NAME.match(name):
        raise ValueError("a savepoint name must be a plain SQL identifier")
    return "SAVEPOINT " + name, "ROLLBACK TO SAVEPOINT " + name, "RELEASE SAVEPOINT " + name


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

    begin, rollback, release = _savepoint_control(name)
    cursor.execute(begin)
    try:
        yield
    except BaseException:
        # 回滚到保存点后还要释放,否则保存点会在事务里越积越多。
        cursor.execute(rollback)
        cursor.execute(release)
        raise
    else:
        cursor.execute(release)


@asynccontextmanager
async def savepoint_async(cursor: Any, name: str = "portable_sql_sp") -> AsyncIterator[None]:
    """`savepoint` 的异步形态:控制语句与回滚语义完全相同,只有 `await` 不同.

    为什么必须有这一份:`await` 不能放进同步上下文管理器里。异步路径(`psycopg` 的
    `AsyncConnection`,workbench 的会话就是它)没有别的办法在唯一冲突后让事务回到可用状态,
    所以「同一机制」在这里表现为同一段控制语句 + 同一套异常规则,而不是第二种插入做法。
    """

    begin, rollback, release = _savepoint_control(name)
    await cursor.execute(begin)
    try:
        yield
    except BaseException:
        await cursor.execute(rollback)
        await cursor.execute(release)
        raise
    else:
        await cursor.execute(release)


#: 唯一约束冲突的 SQLSTATE。标准值,PostgreSQL 与 MySQL 都用它。
_UNIQUE_VIOLATION = "23505"

#: MySQL 不用 SQLSTATE 表达唯一冲突时的 errno 兜底(InnoDB 1062 = ER_DUP_ENTRY)。
_MYSQL_DUP_ENTRY = 1062


def _is_unique_violation(error: BaseException) -> bool:
    """判断异常是否为唯一约束冲突,不导入任何厂商异常类."""

    sqlstate = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
    if sqlstate == _UNIQUE_VIOLATION:
        return True
    args: tuple[Any, ...] = getattr(error, "args", ())
    return len(args) > 0 and args[0] == _MYSQL_DUP_ENTRY


#: 驱动的事务状态里「此刻没有事务块」的那一个。按名字比较,不导入任何厂商枚举类型。
_IDLE_STATUS = "IDLE"


def in_transaction(connection: Any) -> bool:
    """连接上此刻是否有一个保存点可用的事务块(唯一判定点).

    `autocommit` 只说明驱动会不会自动提交**语句**,不说明现在有没有事务块:psycopg 的
    `AsyncConnection` 可以在 `autocommit=True` 时由调用方显式开一个事务块(workbench 的会话
    正是如此),那时 `SAVEPOINT` 合法,而按 `autocommit` 判定会错误地跳过保存点 —— 一次唯一
    冲突就会中止整个事务块。所以先读驱动报告的事务状态(这是适配点,只在这里读),读不到时
    才退回 `autocommit`。

    读不到状态时按「有事务」处理:错开保存点的代价(事务被中止)远大于多开一个保存点的代价。
    """

    status = getattr(getattr(connection, "info", None), "transaction_status", None)
    name = getattr(status, "name", None)
    if isinstance(name, str):
        return name != _IDLE_STATUS
    return not bool(getattr(connection, "autocommit", False))


def _in_autocommit(cursor: Any) -> bool:
    """游标所在的连接现在没有事务块(此时不能开保存点)."""

    return not in_transaction(getattr(cursor, "connection", None))


#: 插入式保存点的名字只在这里出现一次:同步与异步两条路径用的是同一个名字。
_INSERT_SAVEPOINT = "portable_sql_insert_if_absent"


def insert_if_absent(cursor: Any, statement: str, params: Any = ()) -> bool:
    """插入一行;该行已存在时返回 `False`,事务保持可用.

    受限方言既没有 `ON CONFLICT` 也没有 `MERGE`,而「先查后插」在竞争下必然可能撞唯一索引。
    唯一冲突会让 PostgreSQL **中止整个事务**,只捕获 Python 异常不会恢复 —— 所以插入这一步
    必须包在保存点里:冲突时回滚到保存点,事务回到可用状态,调用方可以接着读已有行。

    返回 `True` 表示这一次真的插入了;`False` 表示已存在(并发写者先到,或自己先前插过)。
    其它异常原样抛出。

    **不要用 `cursor.rowcount` 判断结果**:保存点路径的最后一条语句是 `RELEASE SAVEPOINT`,
    它会改写 `rowcount`,所以插入成功后 `rowcount` 可能不是 1。判定只看返回值。

    自动提交(autocommit)连接没有外层事务可保护:此时语句本身就是独立事务,唯一冲突只中止
    这一条语句,不需要也不允许 `SAVEPOINT`(PG 会报 `SAVEPOINT can only be used in
    transaction blocks`)。所以按连接的事务状态决定是否开保存点,而不是按调用方猜测。
    """

    if _in_autocommit(cursor):
        try:
            cursor.execute(statement, params)
        except Exception as error:
            if _is_unique_violation(error):
                return False
            raise
        return True
    try:
        with savepoint(cursor, name=_INSERT_SAVEPOINT):
            cursor.execute(statement, params)
    except Exception as error:
        if _is_unique_violation(error):
            return False
        raise
    return True


async def insert_if_absent_async(cursor: Any, statement: str, params: Any = ()) -> bool:
    """`insert_if_absent` 的异步形态:同一个名字、同一条冲突判定、同一份返回语义.

    这不是第二种插入做法:保存点名字来自 `_INSERT_SAVEPOINT`,冲突判定来自
    `_is_unique_violation`,自动提交分支的理由与同步形态逐条相同。区别只有 `await` ——
    异步连接上的游标无法被同步上下文管理器驱动。
    """

    if _in_autocommit(cursor):
        try:
            await cursor.execute(statement, params)
        except Exception as error:
            if _is_unique_violation(error):
                return False
            raise
        return True
    try:
        async with savepoint_async(cursor, name=_INSERT_SAVEPOINT):
            await cursor.execute(statement, params)
    except Exception as error:
        if _is_unique_violation(error):
            return False
        raise
    return True
