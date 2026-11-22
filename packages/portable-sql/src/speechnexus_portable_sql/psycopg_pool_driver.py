"""把 psycopg_pool 收敛成契约里的 `Pool`.

这里是部署期「驱动 / DSN / profile 选择」的那一处(ADR 0016):业务层只看见
`Pool` 与 `Connection`,池的类型、异常名与等待语义都不外泄。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
from psycopg import IsolationLevel
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

from .contract import ExecutableConnection, PoolUnavailableError
from .psycopg_driver import ContractCursor, PortableConnection, portable_error

__all__ = ["PsycopgPool", "create_pool"]

_POOL_ERRORS = (PoolClosed, PoolTimeout, TooManyRequests)


class PsycopgPool:
    """`ConnectionPool` 的薄包装:只做获取与错误翻译。"""

    __slots__ = ("_pool",)

    def __init__(self, pool: ConnectionPool[Any]) -> None:
        self._pool = pool

    def open(self) -> None:
        self._pool.open()

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self, *, timeout: float | None = None) -> Iterator[ExecutableConnection]:
        try:
            with self._pool.connection(timeout=timeout) as connection:
                yield cast(ExecutableConnection, PortableConnection(connection))
        except _POOL_ERRORS as error:
            # 还没发出语句:与语句失败分开,让调用方按「存储不可用」处理。
            raise PoolUnavailableError("no connection available") from error
        except psycopg.Error as error:
            raise portable_error(error) from error


def create_pool(
    database_url: str,
    *,
    schema: str,
    max_size: int = 4,
    max_waiting: int = 16,
    name: str = "speech-coordination",
    timeout: float = 3,
) -> PsycopgPool:
    """建一个已配置好 search_path / 时区 / 游标工厂的池,但先不打开."""

    def configure(connection: Any) -> None:
        connection.isolation_level = IsolationLevel.READ_COMMITTED
        # PostgreSQL 默认会在可信 schema 之前搜索 pg_temp。
        connection.execute(
            "SELECT set_config('search_path', ?, false)", (schema + ",pg_catalog,pg_temp",)
        )
        connection.execute("SET TIME ZONE 'UTC'")
        connection.commit()

    pool: ConnectionPool[Any] = ConnectionPool[Any](
        database_url,
        kwargs={
            "connect_timeout": 3,
            # 池交给业务的每条连接都按 `?` 契约翻译;业务代码不知道驱动参数标记。
            "cursor_factory": ContractCursor,
        },
        configure=configure,
        name=name,
        min_size=0,
        max_size=max_size,
        max_waiting=max_waiting,
        timeout=timeout,
        open=False,
    )
    return PsycopgPool(pool)
