"""Public job read model; coordination identity stays in its own tables."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.public_job_schema import (
    EVENTS,
    JOBS,
    PUBLIC_JOB_DDL,
)

revision = "0016_public_jobs"
down_revision = "0015_publications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PUBLIC_JOB_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Public job downgrade requires READ COMMITTED isolation")
    # Hold both locks before the emptiness snapshot so no job can appear after it.
    op.execute(f"LOCK TABLE {JOBS}, {EVENTS} IN ACCESS EXCLUSIVE MODE")
    for table in (EVENTS, JOBS):
        if connection.execute(text("SELECT count(*) FROM " + table)).scalar_one():
            raise RuntimeError("Public job history requires preservation and recovery")
    op.execute("DROP TABLE " + EVENTS)
    op.execute("DROP TABLE " + JOBS)
