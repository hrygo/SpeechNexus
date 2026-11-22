"""厂商驱动的唯一入口:业务层从这里取,不直接 import SDK.

为什么需要这层:ADR 0016 把「用哪个驱动、哪个 DSN、哪个 profile」定在部署期,业务代码只认
契约。业务层直接 `import psycopg` 会让换驱动变成全仓搜索,也会让「只在 portable-sql 里允许
品牌依赖」这条边界失效(守卫规则 `vendor-sdk`)。

这里只做再导出,不加任何行为:调用点除了 import 行之外不需要改。
"""

from __future__ import annotations

import psycopg
from psycopg import IsolationLevel, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import class_row, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

__all__ = [
    "ConnectionPool",
    "IsolationLevel",
    "Jsonb",
    "PoolClosed",
    "PoolTimeout",
    "TooManyRequests",
    "class_row",
    "conninfo_to_dict",
    "dict_row",
    "psycopg",
    "sql",
]
