"""建 ``coordination_keyed_locks``:协调命名空间的键锁行表(双角色共用)。

迁移链到 0066 为止没有这张表。要串行化的 catalog 键两侧分别是 backend 与执行网关,它们是
``speech`` schema 里的两个不同角色;锁行若只在一侧够得到的命名空间里,另一侧取不到同一行,
互斥就**静默裂成两把**,而两侧依赖的正是这一份排序。所以表落在双方都已有关系的 ``speech``,
而它此前只存在于键锁原名所在的 workbench 命名空间。

本修订只做一件事:建表。DDL 的唯一定义点是
``speech/infrastructure/coordination_keyed_lock_schema.py``(表名、列名取自
``speechnexus_portable_sql.keyed_lock`` 的清单),授权由
``job_dispatch_grants``(backend)与 ``gateway_query_grants.gateway_keyed_lock_grants``
(网关)在部署期施加 —— 运行时角色不做 DDL,本节也不在启动路径里建表。

``downgrade`` 只 DROP 这张表。表里的行是锁行本身,回到 0066 时它本该不存在;没有别的表
引用它,也没有需要保留的内容。
"""

from __future__ import annotations

from alembic import op

from speechnexus_backend.speech.infrastructure.coordination_keyed_lock_schema import (
    COORDINATION_KEYED_LOCK_DDL,
    COORDINATION_KEYED_LOCK_TABLE,
)

revision = "0067_coordination_keyed_locks"
down_revision = "0066_claim_bucket_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in COORDINATION_KEYED_LOCK_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {COORDINATION_KEYED_LOCK_TABLE}")
