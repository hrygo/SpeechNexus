"""Store the pinned effective configuration accepted with each public Job."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    PUBLIC_JOB_STATUS_GUARD_DDL,
    PUBLIC_JOB_SUBMISSION_DDL,
    SUBMISSIONS,
)

revision = "0017_job_submissions"
down_revision = "0016_public_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PUBLIC_JOB_SUBMISSION_DDL:
        op.execute(statement)
    for statement in PUBLIC_JOB_STATUS_GUARD_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Public Job submission downgrade requires READ COMMITTED isolation")
    op.execute(f"LOCK TABLE {SUBMISSIONS} IN ACCESS EXCLUSIVE MODE")
    if connection.execute(text("SELECT count(*) FROM " + SUBMISSIONS)).scalar_one():
        raise RuntimeError("Public Job submissions require preservation and recovery")
    op.execute("DROP TRIGGER speech_jobs_status_guard ON speech_jobs")
    op.execute("DROP FUNCTION speech_jobs_status_guard()")
    op.execute("DROP TABLE " + SUBMISSIONS)
