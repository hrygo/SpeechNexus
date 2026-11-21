"""连接池契约:业务只拿连接,不认识厂商池类型.

`connection()` 是唯一的取连接入口。拿不到连接时抛 `PoolUnavailableError`,与
「语句执行失败」区分开:那时还没有发出任何语句,事务也没有开始。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from .contract import ExecutableConnection, PoolUnavailableError

__all__ = ["Pool", "PoolUnavailableError"]


@runtime_checkable
class Pool(Protocol):
    """有界连接池的最小表面:打开、取连接、关闭。"""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def connection(
        self, *, timeout: float | None = None
    ) -> AbstractContextManager[ExecutableConnection]: ...
