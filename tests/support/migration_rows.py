"""迁移测试的行快照比较:只比较升级前就存在的列.

为什么需要它:0064 之后,`coordination_jobs` 多了 canonical 列,而迁移测试普遍用
`SELECT *` 抓一份升级前的快照、升级后再抓一份比较,验证「这次迁移不改动既有行」。
加了列之后 `SELECT *` 的键集合变了,即使既有行一个字节都没动,两边也不相等。

那条判据的目的是**取值不变**,不是「不许加列」。所以比较时把升级后的行投影到升级前
已有的键上:既有列必须逐字相等,新增列不参与。需要断言新增列时单独写断言,不要让
「不许加列」这条别的规矩混进「不改既有行」里。

同一形状还有第三类差异:**退役**的列。0070 删掉 `coordination_jobs.claim_expires_at_us`,
它不是被改写,而是整列消失(语义由 canonical `claim_expires_at` 承接,0 = 未领取 → NULL)。
退役列只能按表登记在 `RETIRED_COLUMNS` 这一处,由 `retired=` 显式传到比较点上;不在
登记表里的列一旦缺失就直接报错,不让「不许丢列」这条规矩被顺带放宽。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

#: 退役列,按表分组。只允许登记「升级后按设计不再存在」的列。
RETIRED_COLUMNS: dict[str, frozenset[str]] = {
    "coordination_jobs": frozenset({"claim_expires_at_us"}),
}


def project_onto(
    rows: Sequence[dict[str, Any]],
    reference: Sequence[dict[str, Any]],
    *,
    retired: Collection[str] = (),
) -> list[dict[str, Any]]:
    """把 `rows` 投影到 `reference` 的键集合上.

    `reference` 为空时原样返回:没有参照就无法判断哪些列是新的,这时保持严格比较,
    让调用方自己决定。

    `retired` 列出本次升级按设计退役的列,它们从键集合里剔除;除此之外参照里有的列
    在 `rows` 里缺失就是失败,否则「既有列逐字相等」会退化成「还剩几列就算几列」。
    """

    if not reference:
        return list(rows)
    keys = tuple(key for key in reference[0] if key not in retired)
    if rows:
        missing = tuple(key for key in keys if key not in rows[0])
        if missing:
            raise AssertionError(
                f"columns that are not registered as retired disappeared: {missing}"
            )
    return [{key: row[key] for key in keys} for row in rows]
