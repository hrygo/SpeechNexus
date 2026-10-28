"""Persist gateway abort receipt and dispatch uncertainty."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import ABORT_INTENT_DDL

revision = "0027_gateway_abort_intents"
down_revision = "0025_binding_disable_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(ABORT_INTENT_DDL)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Gateway abort downgrade requires READ COMMITTED isolation")
    op.execute("LOCK TABLE gateway_abort_intents IN ACCESS EXCLUSIVE MODE")
    if connection.execute(text("SELECT EXISTS (SELECT 1 FROM gateway_abort_intents)")).scalar_one():
        raise RuntimeError("Gateway abort intents require preservation")
    op.execute("DROP TABLE gateway_abort_intents")
