"""测试支撑里的 PG 连接入口:与产品代码同一个 `?` 翻译点.

产品 SQL 现在用 `?`,测试自建的连接若仍是裸 psycopg,同一条语句在池上能跑、在测试里红。
这里只注入游标工厂,不包装连接本身(方言初始化需要真实连接)。
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from speechnexus_portable_sql.psycopg_driver import PortableCursor


def connect_kwargs(**overrides: Any) -> dict[str, Any]:
    """连接池的 kwargs:与 `connect()` 同一个翻译点。"""

    kwargs: dict[str, Any] = {"row_factory": dict_row, "connect_timeout": 3}
    kwargs.update(overrides)
    kwargs["cursor_factory"] = PortableCursor
    return kwargs


def connect(*args: Any, **kwargs: Any) -> psycopg.Connection[Any]:
    kwargs.setdefault("cursor_factory", PortableCursor)
    return psycopg.connect(*args, **kwargs)
