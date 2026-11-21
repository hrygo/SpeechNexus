"""PostgreSQL 首发期的驱动适配:把 `?` 参数契约接到 psycopg.

ADR 0016 允许的唯一品牌代码是「部署期 driver / DSN / profile 选择」。这个模块是那个选择点:业务
代码只写 `?` 与 DB-API 语义,由这里翻译给 psycopg;ODBC 驱动接上后业务代码不变,只换这一处。

两条纪律:

1. 只翻译参数标记,不改 SQL 结构、不加品牌函数、不分类 SQLSTATE。
2. 迁移期兼容:整条语句仍是 `%s` 或 `%(name)s` 时原样交给驱动,让 ratchet 继续盯着剩下的语句。

`PortableCursor` 是 psycopg 游标的包装,因此可以直接作为 `cursor_factory` 交给连接池:
池里每条连接、每条语句都经过同一处翻译,业务代码无法绕过。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import psycopg

from .markers import PYFORMAT, qmark_to_pyformat

__all__ = [
    "PYFORMAT",
    "AsyncPortableCursor",
    "PortableConnection",
    "PortableCursor",
    "connect",
    "connect_async",
    "qmark_to_pyformat",
]


def _translated(query: Any, params: Any) -> Any:
    """翻译纯文本与 `psycopg.sql` 组合语句.

    `sql.SQL(...).format(s=schema)` 会把语句拼成 `Composed`,占位符仍留在各个 `SQL` 片段里。
    只翻文本片段、其余节点原样保留,顺序与数量都不变。
    """

    if isinstance(query, str):
        return qmark_to_pyformat(query, params)
    return _translate_composed(query, params)


def _translate_composed(node: Any, params: Any) -> Any:
    if isinstance(node, str):
        return psycopg.sql.SQL(qmark_to_pyformat(node, params))
    if isinstance(node, psycopg.sql.Composed):
        parts = cast(Sequence[Any], node._obj)
        return psycopg.sql.Composed([_translate_composed(part, params) for part in parts])
    if isinstance(node, psycopg.sql.SQL):
        template = node._obj
        return psycopg.sql.SQL(qmark_to_pyformat(template, params))
    return node


class PortableCursor:
    """游标包装:只拦 `execute` / `executemany`,其余行为透传给驱动游标。

    包装而不是子类,是为了让 `cursor_factory` 能拿到 psycopg 交给工厂的全部参数
    (`row_factory` 等),同时不在本模块重复驱动的构造逻辑。
    """

    __slots__ = ("_cursor",)

    def __init__(self, connection: Any, *args: Any, **kwargs: Any) -> None:
        self._cursor = psycopg.Cursor(connection, *args, **kwargs)

    def execute(
        self,
        query: Any,
        params: Any = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> PortableCursor:
        self._cursor.execute(_translated(query, params), params, prepare=prepare, binary=binary)
        return self

    def executemany(self, query: Any, params_seq: Any, *, returning: bool = False) -> None:
        self._cursor.executemany(_translated(query, ()), params_seq, returning=returning)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __enter__(self) -> PortableCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._cursor.close()


class PortableConnection:
    """连接级同样的翻译;事务、只读标记与关闭行为交给真实驱动。"""

    __slots__ = ("_connection",)

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def cursor(self, *args: Any, **kwargs: Any) -> PortableCursor:
        return cast(PortableCursor, self._connection.cursor(*args, **kwargs))

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PortableCursor:
        return cast(
            PortableCursor,
            self._connection.execute(qmark_to_pyformat(sql, params), params),
        )

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

    kwargs.setdefault("cursor_factory", PortableCursor)
    return PortableConnection(psycopg.connect(*args, **kwargs))


class AsyncPortableCursor:
    """异步连接的同一处翻译:业务 SQL 与同步路径完全一致。"""

    __slots__ = ("_cursor",)

    def __init__(self, connection: Any, *args: Any, **kwargs: Any) -> None:
        self._cursor = psycopg.AsyncCursor(connection, *args, **kwargs)

    async def execute(
        self,
        query: Any,
        params: Any = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> AsyncPortableCursor:
        await self._cursor.execute(
            _translated(query, params), params, prepare=prepare, binary=binary
        )
        return self

    async def executemany(self, query: Any, params_seq: Any, *, returning: bool = False) -> None:
        await self._cursor.executemany(_translated(query, ()), params_seq, returning=returning)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    async def __aenter__(self) -> AsyncPortableCursor:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._cursor.close()


async def connect_async(*args: Any, **kwargs: Any) -> Any:
    """与 `psycopg.AsyncConnection.connect` 同签名,游标工厂已接翻译。"""

    kwargs.setdefault("cursor_factory", AsyncPortableCursor)
    return await psycopg.AsyncConnection.connect(*args, **kwargs)
