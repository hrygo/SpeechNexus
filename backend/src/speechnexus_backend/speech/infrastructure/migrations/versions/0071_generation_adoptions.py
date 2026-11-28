"""建账本代次采纳的审计表 ``runtime_generation_adoptions``。

账本与节点各记一套代次,现场读到 1 与 11(#1282)。收敛的第二种做法是不动在跑的引擎、在账本里
把当前代次推进到节点实际准备好的那一代,并把「凭什么可以这么写」留成一行审计。本修订只补那张
表:建表文本的唯一定义点是
``speech/infrastructure/generation_adoption_schema.py``,本文件按序执行它,不在这里写第二份列
清单。授权面在 ``generation_adoption_grants``,由部署期施加;运行时角色不做 DDL,本修订也不在
启动路径里建对象。

表是**只追加**的:主键 ``(instance_id, adopted_generation)`` 让同一次采纳重跑写不进第二行,
授权面没有 ``UPDATE``/``DELETE``。回退一次采纳是回退整笔事实,不是改这一行。

``downgrade`` 只删本修订加的这一张表。此刻它可能已经有行:采纳是操作者显式执行的修复,删表会
连同「哪一代、凭什么、谁采纳的」一起丢掉,所以回退到 ``0070`` 之后**必须**重跑一次采纳
(``speechnexus-adopt-generation``)才能让就绪收据重新可用;``runtime_instance_heads`` 与
``engine_instances`` 的行不在本修订的回退范围里,它们由采纳命令自己写入,回退命令另有其道。
"""

from __future__ import annotations

from alembic import op

from speechnexus_backend.speech.infrastructure.generation_adoption_schema import (
    GENERATION_ADOPTION_DDL,
    GENERATION_ADOPTION_TABLE,
)

revision = "0071_generation_adoptions"
down_revision = "0070_retire_draft_claim_epoch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in GENERATION_ADOPTION_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {GENERATION_ADOPTION_TABLE}")
