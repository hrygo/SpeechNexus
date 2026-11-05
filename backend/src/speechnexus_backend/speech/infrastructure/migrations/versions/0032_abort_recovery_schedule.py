"""Persist fair scheduling without changing original abort receipts."""

from alembic import op

revision = "0032_abort_recovery_schedule"
down_revision = "0031_tts_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gateway_abort_intents ADD COLUMN next_check_at_us BIGINT "
        "NOT NULL DEFAULT 0 CHECK (next_check_at_us >= 0)"
    )
    op.execute(
        "CREATE INDEX gateway_abort_recovery_due ON gateway_abort_intents "
        "((COALESCE(NULLIF(next_check_at_us,0),received_at_us)), submit_intent_id)"
    )


def downgrade() -> None:
    raise RuntimeError("Abort recovery history requires preservation")
