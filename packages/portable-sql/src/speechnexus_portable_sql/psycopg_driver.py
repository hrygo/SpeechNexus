"""PostgreSQL 首发期的驱动适配:把 `?` 参数契约接到 psycopg.

ADR 0016 允许的唯一品牌代码是「部署期 driver / DSN / profile 选择」。这个模块就是那个选择点:
业务代码只写 `?` 与 DB-API 语义,由这里翻译给 psycopg。真正的 ODBC 驱动接上之后,业务代码不变,
只会替换这一处。

两条纪律:

1. 只翻译参数标记,不改 SQL 结构、不加品牌函数、不分类 SQLSTATE。
2. 迁移期兼容:整条语句仍是 `%s` 或 `%(name)s` 时原样交给驱动,让 ratchet 继续盯着剩下的语句。

psycopg 只在本模块被导入:这是 guard 的 driver 例外,不是业务层的放行。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

__all__ = ["PYFORMAT", "PortableConnection", "PortableCursor", "connect", "qmark_to_pyformat"]

PYFORMAT = re.compile(r"%\([a-z_][a-z0-9_]*\)s", re.IGNORECASE)


def qmark_to_pyformat(sql: str, params: Sequence[Any] | None) -> str:
    """把 qmark 语句翻成 psycopg 的 `%s`;已有 `%` 一律按字面量转义。"""

    if params is None or "?" not in sql:
        return sql
    if "%s" in sql or PYFORMAT.search(sql):
        return sql
    return sql.replace("%", "%%").replace("?", "%s")


class PortableCursor:
    """只改 `execute` / `executemany` 的游标;其余行为原样透传。"""

    __slots__ = ("_cursor",)

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PortableCursor:
        self._cursor.execute(qmark_to_pyformat(sql, params), params if params is not None else None)
        return self

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> PortableCursor:
        self._cursor.executemany(qmark_to_pyformat(sql, ()), seq)
        return self

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __enter__(self) -> PortableCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._cursor.close()


class PortableConnection:
    """连接级同样的翻译;事务、只读标记与关闭行为交给真实驱动。"""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def cursor(self, *args: Any, **kwargs: Any) -> PortableCursor:
        return PortableCursor(self._connection.cursor(*args, **kwargs))

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PortableCursor:
        translated = qmark_to_pyformat(sql, params)
        cursor = self._connection.execute(translated, params if params is not None else None)
        return PortableCursor(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    @property
    def read_only(self) -> bool:
        return bool(self._connection.read_only)

    @read_only.setter
    def read_only(self, value: bool) -> None:
        self._connection.read_only = value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __enter__(self) -> PortableConnection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._connection.close()


def connect(*args: Any, **kwargs: Any) -> PortableConnection:
    """与 `psycopg.connect` 同签名;返回按 `?` 契约翻译的连接。"""

    import psycopg

    return PortableConnection(psycopg.connect(*args, **kwargs))
