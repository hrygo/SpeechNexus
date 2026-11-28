"""``runtime_generation_adoptions`` 的运行授权面:一次采纳只追加一行审计。

**这一份为什么比同类授权模块短。** 采纳命令写到的其它关系全部是**注册路径已经在写的**:
``engine_instances`` 的插入、``runtime_instance_heads`` / ``runtime_instance_intents`` /
``resource_pools`` 的更新、``coordination_outbox`` 与 ``runtime_intent_deliveries`` 的插入,
这些列级面由 ``coordination_grants.coordination_runtime_grants`` 一次声明,本模块不复制第二份
——复制一份就等于给同一条路径两个定义点,加列时必然漏一处。采纳在这种身份上新增的权限恰好只有
一件事:向审计表追加一行。

**读者与写者分开。** 审计是给人看的:``generation_adoption_reader_grants`` 只授 ``SELECT``,
供只读审计身份读取「哪一代、凭什么、谁采纳的」;写者侧没有 ``UPDATE``/``DELETE``,采纳行是
历史事实,不可改写。两条函数由同一份列清单派生,列改名只需要改
``generation_adoption_schema``。

**当前没有调用点,这是刻意的。** 参考部署里 ``speechnexus-register-engine`` 与
``speechnexus-adopt-generation`` 都以 bootstrap owner 凭据运行(``register-engine-job.yaml``
的 ``registration-credentials``;``docs/operations/compose-minimal.md`` 把「生产须换独立
operator 凭据」列为未完成项),所以采纳现在靠 owner 权限落库。本模块是那条路径**已经需要用到的
最小面**的声明:换成独立 operator 身份时,只施加这两条函数,不把协调表的写权整表发出去,也不
需要回头再想「它到底写了哪些列」。
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.generation_adoption_schema import (
    GENERATION_ADOPTION_INSERT_COLUMNS,
    GENERATION_ADOPTION_TABLE,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name


def generation_adoption_writer_grants(schema: str, role: str) -> tuple[str, ...]:
    """采纳写者:审计表追加一行。其它关系沿用协调运行角色已有的列级面。"""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    columns = ", ".join(GENERATION_ADOPTION_INSERT_COLUMNS)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT INSERT ({columns}) ON "{schema}".{GENERATION_ADOPTION_TABLE} TO "{role}"',
    )


def generation_adoption_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    """审计读者:只读,没有 ``INSERT``/``UPDATE``/``DELETE``。"""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".{GENERATION_ADOPTION_TABLE} TO "{role}"',
    )


__all__ = [
    "generation_adoption_reader_grants",
    "generation_adoption_writer_grants",
]
