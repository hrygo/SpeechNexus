"""``runtime_readiness_voids`` 的运行授权面:一次作废只追加一行。

**这份为什么短。** 作废发生在续租路径上,那条路径已经持有 ``engine_instances`` /
``runtime_instance_heads`` / ``runtime_instance_intents`` / ``resource_pools`` 的列级写权
(``coordination_grants.coordination_runtime_grants`` 一次声明),本模块不复制第二份。续租在这种
身份上新增的权限恰好只有一件事:向作废审计表追加一行。

**读者与写者分开。** 报告(``speechnexus-engine-readiness``)以只读会话运行,它只需要 ``SELECT``;
写者侧没有 ``UPDATE``/``DELETE``,作废行是历史事实,不可改写。两条函数都从同一份列清单派生,
加列只改 ``readiness_void_schema``。

**当前没有独立身份调用点,这是刻意的。** 参考装配里续租以 bootstrap owner 凭据运行(见
``docs/operations/compose-minimal.md`` 的未完成项),所以作废目前靠 owner 权限落库。本模块是那条
路径**已经需要**的最小写面:换成独立 operator 身份时只施加这两条函数,不把协调表整表写权发出去。
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.readiness_void_schema import (
    READINESS_VOID_INSERT_COLUMNS,
    READINESS_VOID_TABLE,
)


def readiness_void_writer_grants(schema: str, role: str) -> tuple[str, ...]:
    """作废写者:审计表追加一行,没有 ``UPDATE``/``DELETE``。"""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    columns = ", ".join(READINESS_VOID_INSERT_COLUMNS)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT INSERT ({columns}) ON "{schema}".{READINESS_VOID_TABLE} TO "{role}"',
    )


def readiness_void_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    """作废读者:只读。准入与报告都只读这张表。"""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".{READINESS_VOID_TABLE} TO "{role}"',
    )


__all__ = ["readiness_void_reader_grants", "readiness_void_writer_grants"]
