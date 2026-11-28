"""作废与收据先后关系的唯一判定(#1286)。

检测到确定不一致时,检测方在 ``runtime_readiness_voids`` 追加一行;准入与报告据此判断「这张收据
还算不算数」。判定只有一条:**作废时刻不早于收据的签发时刻,作废就适用于它**。

这条规则放在 domain 而不是各自实现,因为报告与准入必须对同一个瞬间给出同一个答案(#863):两边
各写一份比较,先漂的是边界(``>=`` 还是 ``>``),再漂的就是「报告说 valid、准入说不行」。

它同时也是「作废可被后续成功续租覆盖」的机制:续租签的新收据 ``verified_at_us`` 严格晚于既有
``voided_at_us``(两者都取数据库时钟),于是同一条规则自动判定「这条作废与这张新收据无关」,
不需要撤销动作,也不会永久毒化。想绕过它就只能改写历史行,而那没有授权面。
"""

from __future__ import annotations


def void_invalidates_receipt(*, voided_at_us: int | None, receipt_verified_at_us: int) -> bool:
    """``voided_at_us`` 这次作废是否让这张收据失效。

    没有作废行(``None``)永远不失效。时刻相等按「失效」处理:同一微秒内先签后作废意味着作废是
    后写的事实,宁可拒绝也不放行——放行一张已判定不一致的收据正是本切片要关的窗口。
    """
    if voided_at_us is None:
        return False
    return voided_at_us >= receipt_verified_at_us


__all__ = ["void_invalidates_receipt"]
