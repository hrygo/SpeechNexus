"""Retain transport progress without treating old outbox rows as never sent."""

from alembic import op

from speechnexus_backend.speech.infrastructure.runtime_delivery_schema import (
    ADOPT_RUNTIME_DELIVERIES_SQL,
    RUNTIME_DELIVERY_DDL,
)

revision = "0008_runtime_delivery"
down_revision = "0007_admission_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in RUNTIME_DELIVERY_DDL:
        op.execute(statement)
    # Adopt all old rows at one stable migration-transaction timestamp.
    # This is metadata, never a dispatch or physical-observation fact.
    clock = "CAST(EXTRACT(EPOCH FROM transaction_timestamp()) * 1000000 AS BIGINT)"
    # 常量已迁到 `?` 契约;迁移把唯一的占位符内联成一个稳定事务时间戳。
    op.execute(ADOPT_RUNTIME_DELIVERIES_SQL.replace("?", clock))


def downgrade() -> None:
    raise RuntimeError("Destructive runtime delivery downgrade requires explicit history recovery")
