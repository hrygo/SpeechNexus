"""参数标记的唯一翻译定义:契约用 `?`,驱动用各自的风格.

为什么单独一层:psycopg 与 pymysql 都需要同一条规则(含「字面量 `%` 先转义」),把规则写两遍
就是两份实现的开始。驱动模块只负责把连接的游标接到这里。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

__all__ = ["PYFORMAT", "qmark_to_pyformat"]

PYFORMAT = re.compile(r"%\([a-z_][a-z0-9_]*\)s", re.IGNORECASE)


def qmark_to_pyformat(sql: str, params: Sequence[Any] | None) -> str:
    """把 qmark 语句翻成 `%s`;已有 `%` 按字面量转义,旧语句原样放过。"""

    if params is None or "?" not in sql:
        return sql
    if "%s" in sql or PYFORMAT.search(sql):
        return sql
    return sql.replace("%", "%%").replace("?", "%s")
