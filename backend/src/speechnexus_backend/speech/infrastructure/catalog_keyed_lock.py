"""目录身份互斥:在 `coordination_keyed_locks` 上按目录身份取行锁.

catalog 族此前是 `pg_advisory_xact_lock(hashtextextended('speechnexus:catalog:' ||
current_schema() || ':<kind>:' || <id>'))`。它是**同一把**逻辑锁:发布、退役、撤销、来源
批准、TTS 准入、探针授权/批准、操作员认证都在这把锁下判定「我看到的目录事实仍是最新的」。
所以替代品也必须是同一张表上的同一行,而不是每处各找一条天然行 —— 参与者里有几处在目录
事实**还不存在**时运行(来源批准、探针授权、操作员认证),而 `SELECT … FOR UPDATE` 在行不
存在时两引擎都不锁任何东西,那样换出来的不是「弱一点的锁」,是**静默失效**。

**这里是唯一的取锁点。** 键文本只由 `speechnexus_portable_sql.keyed_lock` 拼
(`catalog_lock_key` / `voice_lock_key`),语句只由它的 `lock_statements` 生成;本模块不再写
一份键前缀,也不另写一条 SQL。—— 端点各不相同(14 个站点),载体只有一个。

锁序。这把行锁是该事务的**第一把行锁**,一直持到提交,顺序固定为

    coordination_keyed_locks(binding 键) -> coordination_keyed_locks(voice 键)
      -> 准入容量/租约行 -> claim bucket -> canonical Job -> public Job/outbox

绑定在前、声音在后,所有参与者一致(`builtin_voice_publication`、`qualification_probe_*`、
`qualification_admission`、`operator_attested_qualification` 都是这个次序)。同一事务里重复取
同一个键不自阻塞:桶行已插、行锁已在手,第二次 INSERT 走唯一冲突分支,`SELECT … FOR UPDATE`
锁的还是自己持有的那一行。

绑定族的取锁入口是 `binding_disable_barrier.lock_binding`,它是跨进程那一侧(执行网关也调
它),所以绑定键**不在**本模块再开一个入口;本模块负责通用 `(kind, object_id)` 站点与声音族。
"""

from __future__ import annotations

from typing import Any

from speechnexus_portable_sql.keyed_lock import (
    COORDINATION_KEYED_LOCK_TABLE,
    acquire_in,
    catalog_lock_key,
    voice_lock_key,
)

__all__ = ["lock_catalog", "lock_voice"]


def lock_catalog(executor: Any, kind: str, object_id: str) -> None:
    """在 `executor` 的事务里锁住目录身份 `(kind, object_id)` 的那一行.

    `executor` 可以是连接,也可以是调用方手上那个游标(包装过的游标同样可以:它自带
    `execute`,`acquire_in` 会用它所属的连接判定事务)。生产路径两种形态都有 ——
    `publications_sql` 收到的是游标,`qualification_*` 收到的是包装过截止时间的游标。
    """

    acquire_in(
        executor,
        key=catalog_lock_key(kind, object_id),
        table=COORDINATION_KEYED_LOCK_TABLE,
    )


def lock_voice(executor: Any, voice_id: str) -> None:
    """锁住声音身份 `voice_id` 的那一行(它与绑定行同表、不同行)."""

    acquire_in(
        executor,
        key=voice_lock_key(voice_id),
        table=COORDINATION_KEYED_LOCK_TABLE,
    )
