"""keyset 分页:不依赖 `LIMIT/OFFSET`。

SQL 侧用 `(sort_key, id)` 条件与 `ORDER BY` 同向排序,多取一行判断是否还有下一页;
本模块只负责「多取一行」的切分,不生成 SQL。
"""

from __future__ import annotations

from collections.abc import Sequence

from speechnexus_portable_sql.contract import ContractError, Row

__all__ = ["keyset_page"]


def keyset_page(rows: Sequence[Row], page_size: int) -> tuple[list[Row], bool]:
    """把 `fetchmany(page_size + 1)` 的结果切成 `(page, has_more)`。"""

    if page_size < 1:
        raise ContractError("page_size must be at least 1")
    if len(rows) > page_size + 1:
        raise ContractError("fetch at most page_size + 1 rows")
    return list(rows[:page_size]), len(rows) > page_size
