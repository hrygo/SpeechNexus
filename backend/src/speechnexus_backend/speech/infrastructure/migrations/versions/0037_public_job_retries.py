"""Attach replacement lineage to public Jobs; parents stay terminal."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.public_job_retry_schema import (
    PUBLIC_JOB_RETRY_DDL,
    PUBLIC_JOB_RETRY_GUARD_REPLACE,
)
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    PUBLIC_JOB_STATUS_GUARD_REPLACE,
)

revision = "0037_public_job_retries"
down_revision = "0036_abort_dispatch_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PUBLIC_JOB_RETRY_DDL:
        op.execute(statement)
    # The guard is replaced only after the column exists; the 0016 definition
    # cannot reference a column added by this revision.
    op.execute(PUBLIC_JOB_RETRY_GUARD_REPLACE)


def downgrade() -> None:
    connection = op.get_bind()
    # Freeze the rows before deciding whether the older shape can represent them;
    # a concurrent retry must not appear after the check.
    op.execute("LOCK TABLE speech_jobs IN ACCESS EXCLUSIVE MODE")
    if connection.execute(
        text("SELECT count(*) FROM speech_jobs WHERE retry_of_job_id IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("Public Job replacement lineage requires preservation")
    op.execute("DROP INDEX speech_jobs_retry_of")
    op.execute("ALTER TABLE speech_jobs DROP CONSTRAINT speech_jobs_retry_of_check")
    op.execute("ALTER TABLE speech_jobs DROP COLUMN retry_of_job_id")
    # Restore the 0034 guard; the replaced body would otherwise read a column
    # that no longer exists.
    op.execute(PUBLIC_JOB_STATUS_GUARD_REPLACE)
