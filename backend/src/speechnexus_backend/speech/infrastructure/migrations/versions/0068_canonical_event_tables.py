"""建 canonical ``job_events``,并给 ``coordination_outbox`` 加 ``delivered_at``。

事件侧切 canonical 之前必须先有对象。canonical 形状从
``migrations/0003_portable_claim_buckets.sql`` 起就声明了 ``job_events`` 与
``coordination_outbox.delivered_at``,但 alembic 迁移链到 ``0067`` 为止**没有这张表**,outbox 上
只有草案的 ``delivered_at_us BIGINT``;事件读路径仍在草案 ``speech_job_events`` 上,所以缺口一直
没有以 42P01 显形。本修订只补对象(计划 §5 S0):建表、加列,**没有写者,也没有读者**。

DDL 的唯一定义点是
``speech/infrastructure/canonical_event_schema.py``:建表文本、列清单与授权面都在那里,本文件只
按序执行它们。授权由 ``canonical_event_grants`` 在部署期施加(它现在还没有调用点,见该模块的
docstring);运行时角色不做 DDL,本节也不在启动路径里建对象。

``IF NOT EXISTS`` 与迁移树其余部分一致:若干集成用例把 ``alembic_version`` 回退再
``upgrade head``,那时对象可能已经在。``delivered_at_us`` 原样保留 —— 它仍有读者,删列是单向
步骤,属于写者切换之后的观察窗(计划 D2/S5),不是本修订。

``downgrade`` 只删本修订加的两样东西:outbox 的 ``delivered_at`` 列与 ``job_events`` 表。两列
此刻都没有写者,所以回退不会丢事实;``delivered_at_us`` 与 ``coordination_jobs`` 的行都不动。
"""

from __future__ import annotations

from alembic import op

from speechnexus_backend.speech.infrastructure.canonical_event_schema import (
    COORDINATION_OUTBOX_TABLE,
    JOB_EVENT_DDL,
    JOB_EVENT_TABLE,
    OUTBOX_DELIVERED_AT_COLUMN,
)

revision = "0068_canonical_event_tables"
down_revision = "0067_coordination_keyed_locks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in JOB_EVENT_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE {COORDINATION_OUTBOX_TABLE} "
        f"DROP COLUMN IF EXISTS {OUTBOX_DELIVERED_AT_COLUMN}"
    )
    op.execute(f"DROP TABLE IF EXISTS {JOB_EVENT_TABLE}")
