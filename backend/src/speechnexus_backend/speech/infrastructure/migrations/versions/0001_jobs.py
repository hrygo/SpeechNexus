"""Production coordination Job identity and atomic initial outbox fact."""

from alembic import op

from speechnexus_backend.speech.infrastructure.job_schema import JOB_DDL

revision = "0001_jobs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in JOB_DDL:
        op.execute(statement)


def downgrade() -> None:
    # These tables contain durable execution/idempotency evidence. Roll back
    # application code or restore an approved backup; never silently erase them.
    raise RuntimeError("Destructive Job/outbox downgrade requires an explicit recovery plan")
