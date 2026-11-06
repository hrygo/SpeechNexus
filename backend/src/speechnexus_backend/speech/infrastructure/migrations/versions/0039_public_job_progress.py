"""Store observed public Job progress as ordered facts in the event stream.

The ``kind`` column is added with ``DEFAULT 'lifecycle'``: every event row
committed before this revision is a lifecycle transition, so the backfill marks
it as such and leaves the progress columns NULL.

``downgrade`` refuses once any progress fact exists, because the older shape
cannot represent an observed progress fact. Progress facts are execution
evidence and are never deleted, so the first recorded fact makes an in-place
rollback to 0038 permanent: recovery means rolling forward or restoring a
backup taken before this revision, never dropping the progress columns.
"""

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
