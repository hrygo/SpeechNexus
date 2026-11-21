"""MySQL/InnoDB 验证路径的驱动适配:把 `?` 契约接到 pymysql.

与 psycopg 驱动共用 `markers.qmark_to_pyformat`:同一条规则不写第二遍。MySQL 只是验证协调
路径的第二个引擎,不是首发数据库;这里同样不做品牌分支,只翻译参数标记。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pymysql

from .markers import qmark_to_pyformat

__all__ = ["PortableConnection", "connect"]


class _PortableCursor:
    __slots__ = ("_cursor",)

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _PortableCursor:
        self._cursor.execute(qmark_to_pyformat(sql, params), params)
        return self

    def executemany(self, sql: str, seq: Any) -> _PortableCursor:
        self._cursor.executemany(qmark_to_pyformat(sql, ()), seq)
        return self

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __enter__(self) -> _PortableCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._cursor.close()


class PortableConnection:
    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def cursor(self, *args: Any, **kwargs: Any) -> _PortableCursor:
        return _PortableCursor(self._connection.cursor(*args, **kwargs))

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __enter__(self) -> PortableConnection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._connection.close()


def connect(*args: Any, **kwargs: Any) -> PortableConnection:
    return PortableConnection(pymysql.connect(*args, **kwargs))
