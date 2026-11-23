"""迁移测试的行快照比较:只比较升级前就存在的列.

为什么需要它:0064 之后,`coordination_jobs` 多了 canonical 列,而迁移测试普遍用
`SELECT *` 抓一份升级前的快照、升级后再抓一份比较,验证「这次迁移不改动既有行」。
加了列之后 `SELECT *` 的键集合变了,即使既有行一个字节都没动,两边也不相等。

那条判据的目的是**取值不变**,不是「不许加列」。所以比较时把升级后的行投影到升级前
已有的键上:既有列必须逐字相等,新增列不参与。需要断言新增列时单独写断言,不要让
「不许加列」这条别的规矩混进「不改既有行」里。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def project_onto(
    rows: Sequence[dict[str, Any]], reference: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把 `rows` 投影到 `reference` 的键集合上.

    `reference` 为空时原样返回:没有参照就无法判断哪些列是新的,这时保持严格比较,
    让调用方自己决定。
    """

    if not reference:
        return list(rows)
    keys = tuple(reference[0])
    return [{key: row[key] for key in keys} for row in rows]
