"""canonical ``job_events`` 的读侧:续读语句与列清单都不在这里重新定义。

S2 的读翻转把「哪些事件存在、它们的身份是什么」交给 canonical。这里只做一件事:执行
``speechnexus_portable_sql.claims.STATEMENTS["job_event.read"]``,把它返回的行翻译成类型化的
``CanonicalJobEvent``。语句文本、列清单与排序都不在本模块 —— 它们各自只有一个定义点
(``claims`` 里的语句、``canonical_event_schema`` 里的授权面),本模块只是那条语句的调用点。

**为什么不直接复用 ``claims.read_events``。** 那个函数的连接协议是 ``Row.at(index)``
(``ContractCursor`` 的行),而产品侧的 ``speech_pool`` 用 ``row_factory=dict_row``,行是映射。
两边都包一层只会造出第二份「读事件」的实现,所以这里按语句 + 映射行写一次,语句仍然只有一份。
``payload_ref`` 按产品决定恒为 ``NULL``(搬迁不引用外部负载),这里原样返回,不做默认值填充。

**读面。** 列级 ``SELECT`` 必须同时覆盖投影与谓词列(``WHERE job_id = ?``),否则整条语句以
``42501`` 失败 —— 授权面由 ``canonical_event_schema.job_event_grants`` 声明,S2 补上了
``job_id``。本模块不自己列权限,也不捕获 ``42501``:缺授权是部署缺陷,应该在第一次读时以存储
错误显形,而不是被翻译成「没有事件」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from speechnexus_portable_sql.claims import STATEMENTS

__all__ = ["CanonicalJobEvent", "read_job_events"]


class _Rows(Protocol):
    def fetchall(self) -> list[dict[str, Any]]: ...


class _Executor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Rows: ...


@dataclass(frozen=True, slots=True)
class CanonicalJobEvent:
    """canonical 事件行:序号、摘要,以及按产品决定恒为空的 ``payload_ref``。

    没有 ``state``/``occurred_at``/``kind``/``progress_*`` —— canonical 不存这些列,摘要覆盖了
    它们但摘要不可逆(计划 §3.4)。所以任何需要这些列的读者仍要读草案行;本类型只回答
    「这个 Job 有哪些事件、顺序如何、身份(摘要)是什么」。
    """

    event_seq: int
    event_digest: str
    payload_ref: str | None = None


def read_job_events(
    connection: _Executor, *, job_id: str, after_seq: int
) -> tuple[CanonicalJobEvent, ...]:
    """按 ``(job_id, event_seq)`` 续读 canonical 事件,``event_seq`` 严格大于 ``after_seq``。

    ``after_seq`` 是持久序号,不是线路 ``id``:线路 ``id`` 是 ``event_seq + 1``,翻译只发生在
    HTTP 边界(``jobs._last_event_id``)。
    """

    rows = connection.execute(STATEMENTS["job_event.read"], (job_id, after_seq)).fetchall()
    return tuple(
        CanonicalJobEvent(
            event_seq=int(row["event_seq"]),
            event_digest=str(row["event_digest"]),
            payload_ref=None if row["payload_ref"] is None else str(row["payload_ref"]),
        )
        for row in rows
    )
