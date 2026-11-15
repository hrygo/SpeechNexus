"""Record when an Artifact's content was removed.

Retention already moves a row to ``deleted`` after a confirmed object removal,
but that state alone cannot say *when* the bytes went away, so the removal was
only visible in the process log. The repository's deletion rule asks for a
persistent ledger: ``deleted_at_us`` is written by the same statement that makes
the row ``deleted``, and the added check keeps the state and the timestamp
inseparable, so a row can neither claim a removal without a time nor carry a
removal time while still claiming another state.

Rows a deployment already marked ``deleted`` are backfilled from their own
creation time and the migration clock, so the check can be installed without
refusing an existing database.

The retention grant grows to exactly this column
(``artifact_retention_grants``); deadlines stay unwritable by the sweep.
"""

from alembic import op

from speechnexus_backend.speech.infrastructure.artifact_schema import (
    ARTIFACT_DELETED_LEDGER_DDL,
)

revision = "0054_artifact_deleted_ledger"
down_revision = "0053_voice_draft_publication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in ARTIFACT_DELETED_LEDGER_DDL:
        op.execute(statement)


def downgrade() -> None:
    # The column is the removal record; dropping it forgets when bytes went away,
    # which is why this is only safe before a retention pass has run.
    op.execute(
        "ALTER TABLE speech_artifacts DROP CONSTRAINT IF EXISTS "
        "speech_artifacts_deleted_ledger_check"
    )
    op.execute("ALTER TABLE speech_artifacts DROP COLUMN IF EXISTS deleted_at_us")
