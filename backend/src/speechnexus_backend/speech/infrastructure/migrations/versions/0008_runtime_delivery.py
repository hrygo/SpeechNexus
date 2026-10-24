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
    op.execute(ADOPT_RUNTIME_DELIVERIES_SQL.replace("%s", clock))


def downgrade() -> None:
    raise RuntimeError("Destructive runtime delivery downgrade requires explicit history recovery")
