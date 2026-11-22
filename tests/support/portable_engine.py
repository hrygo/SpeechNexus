"""测试里创建 SQLAlchemy 引擎的唯一入口:连接按 `?` 契约翻译.

产品 SQL 会被 `text()` 原样交给驱动,所以测试自建的引擎也必须经过同一个
`PortableCursor`;否则同一条语句在池上能跑、在引擎上红。#1100 的收敛要求两条路径都对。
"""

from __future__ import annotations

from typing import Any

import psycopg
from sqlalchemy import Engine, create_engine

from speechnexus_portable_sql.psycopg_driver import PortableCursor
from tests.support.portable_pg import connect as portable_pg_connect


def portable_engine(url: str, **kwargs: Any) -> Engine:
    """`create_engine` 的替代:每个 DBAPI 连接都返回按 qmark 翻译的包装."""

    connect_kwargs: dict[str, Any] = {"cursor_factory": PortableCursor}
    for key in ("autocommit", "connect_timeout"):
        if key in kwargs:
            connect_kwargs[key] = kwargs.pop(key)

    def creator() -> psycopg.Connection[Any]:
        # 连接本身保持真实 psycopg 连接(方言初始化会直接读它),只换游标工厂。
        return portable_pg_connect(url, **connect_kwargs)

    return create_engine(url, creator=creator, **kwargs)
