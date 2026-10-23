"""Separate recorded intents from observations and support their durable dispatch."""

from alembic import op

from speechnexus_backend.speech.infrastructure.runtime_intent_schema import RUNTIME_INTENT_DDL

revision = "0005_runtime_intents"
down_revision = "0004_asset_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in RUNTIME_INTENT_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Destructive runtime intent downgrade requires explicit history recovery")
