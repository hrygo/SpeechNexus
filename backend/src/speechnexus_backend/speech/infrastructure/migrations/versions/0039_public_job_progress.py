"""Store observed public Job progress as ordered facts in the event stream."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.public_job_progress_schema import (
    EVENTS,
    PUBLIC_JOB_PROGRESS_DDL,
    PUBLIC_JOB_PROGRESS_DROP,
)

revision = "0039_public_job_progress"
down_revision = "0038_voice_catalogue_reader"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PUBLIC_JOB_PROGRESS_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    # Freeze the event history before deciding whether the older shape can
    # represent it; a progress fact must not appear after the check.
    op.execute(f"LOCK TABLE {EVENTS} IN ACCESS EXCLUSIVE MODE")
    if connection.execute(
        text(f"SELECT count(*) FROM {EVENTS} WHERE kind = 'progress'")
    ).scalar_one():
        raise RuntimeError("Public Job progress facts require preservation")
    for statement in PUBLIC_JOB_PROGRESS_DROP:
        op.execute(statement)
