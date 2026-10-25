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
    # Alembic has already read its version/mutex inside this transaction.
    # A repeatable snapshot could predate a writer that commits while LOCK waits.
    # Reject it rather than count stale history after acquiring the table locks.
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Binding default downgrade requires READ COMMITTED isolation")
    # Match the normal writer's command -> revision -> snapshot lock order.
    # Counts alone take ACCESS SHARE and cannot exclude an in-flight/new writer.
    # Hold these locks through both checks and DDL in the migration transaction:
    # an earlier writer is observed after commit; a later one cannot succeed
    # against a schema that is being removed.
    op.execute(
        "LOCK TABLE speech_binding_commands, speech_binding_revisions, "
        "speech_binding_defaults IN ACCESS EXCLUSIVE MODE"
    )
    snapshots = connection.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar_one()
    declared = connection.execute(
        text("SELECT count(*) FROM speech_binding_commands WHERE defaults_declared IS TRUE")
    ).scalar_one()
    if snapshots or declared:
        raise RuntimeError("Binding default history downgrade requires preservation and recovery")
    op.execute(f"DROP TABLE {TABLE}")
    op.execute(f"ALTER TABLE speech_binding_revisions DROP CONSTRAINT {PARENT_KEY}")
    op.execute("ALTER TABLE speech_binding_commands DROP COLUMN defaults_declared")
