"""建就绪收据作废的审计表 ``runtime_readiness_voids``(#1286)。

准入判断的依据是**已签发的收据**:账本与节点再次分歧时,那张旧收据在 ``expires_at_us`` 之前仍然
``valid``,``create`` 照样放行——报告说红、准入说行的窗口宽度就是收据 TTL。本修订只补那张审计表:
建表文本的唯一定义点是 ``speech/infrastructure/readiness_void_schema.py``,本文件按序执行它,
不在这里写第二份列清单。授权面在 ``readiness_void_grants``,由部署期施加;运行时角色不做 DDL,
本修订也不在启动路径里建对象。

表是**只追加**的:主键 ``(instance_id, instance_generation, observed_generation)`` 让同一次分歧
的重复检测(续租每 5 分钟一次)写不进第二行,授权面没有 ``UPDATE``/``DELETE``。恢复不做「撤销
作废」:下一次成功续租签的新收据 ``verified_at_us`` 晚于 ``voided_at_us``,作废就不再适用于它。

``downgrade`` 只删本修订加的这一张表。此刻它可能已经有行:作废是检测到真实分歧后写的审计,
删表会连同「哪一代对哪一代、凭什么、什么时候」一起丢掉,所以回退到 ``0071`` 之后必须让续租重新
签一次收据(收据本身在 ``runtime_instance_intents.readiness_document`` 里,不随本表回退)。
"""

from __future__ import annotations

from alembic import op

from speechnexus_backend.speech.infrastructure.readiness_void_schema import (
    READINESS_VOID_DDL,
    READINESS_VOID_TABLE,
)

revision = "0072_readiness_voids"
down_revision = "0071_generation_adoptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in READINESS_VOID_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {READINESS_VOID_TABLE}")
