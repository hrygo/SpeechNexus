"""PG 上跑 `?` 参数契约的占位驱动:把 qmark 适配到 psycopg 的 `%s`.

这不是产品路径,也不是品牌适配层:#1100/#1101 会换成 pyodbc,业务代码里的 SQL 与参数顺序
不变,只有这个测试替身消失。它存在的唯一理由,是在 ODBC 驱动接上之前就能用真实数据库验证
claim bucket、fencing 与事件序号的并发语义。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from speechnexus_portable_sql.contract import Cursor, Params, Row

__all__ = ["QmarkConnection", "QmarkCursor"]


class QmarkCursor:
    __slots__ = ("_cursor", "_description")

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._description: Sequence[Any] = ()

    def execute(self, sql: str, params: Params = ()) -> None:
        if "?" not in sql:
            self._cursor.execute(sql, params)
        else:
            self._cursor.execute(sql.replace("?", "%s"), params)
        self._description = self._cursor.description or ()

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def fetchall(self) -> list[Row]:
        names = tuple(str(column.name) for column in self._description)
        return [Row(names, tuple(row)) for row in self._cursor.fetchall()]

    def fetchmany(self, size: int) -> list[Row]:
        rows = self.fetchall()
        return rows[:size]

    def fetchone(self) -> Row | None:
        rows = self.fetchall()
        return rows[0] if rows else None

    def close(self) -> None:
        self._cursor.close()


class QmarkConnection:
    """最小 `Connection` 协议实现;事务由调用方显式提交或回滚。"""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def cursor(self) -> Cursor:
        return QmarkCursor(self._connection.cursor())

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()
