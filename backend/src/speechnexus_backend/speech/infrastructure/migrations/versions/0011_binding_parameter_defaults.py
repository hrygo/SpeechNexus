"""Atomically attach explicit declarations to new binding revisions only."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.binding_defaults_schema import (
    BINDING_DEFAULTS_DDL,
    PARENT_KEY,
    TABLE,
)

revision = "0011_binding_parameter_defaults"
down_revision = "0010_model_binding_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in BINDING_DEFAULTS_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    snapshots = connection.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar_one()
    declared = connection.execute(
        text("SELECT count(*) FROM speech_binding_commands WHERE defaults_declared IS TRUE")
    ).scalar_one()
    if snapshots or declared:
        raise RuntimeError("Binding default history downgrade requires preservation and recovery")
    op.execute(f"DROP TABLE {TABLE}")
    op.execute(f"ALTER TABLE speech_binding_revisions DROP CONSTRAINT {PARENT_KEY}")
    op.execute("ALTER TABLE speech_binding_commands DROP COLUMN defaults_declared")
