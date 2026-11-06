"""Support stable workspace-scoped Job keyset pages, including timestamp ties."""

from alembic import op

revision = "0040_public_job_listing"
down_revision = "0039_public_job_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX speech_jobs_workspace_created_id "
        "ON speech_jobs (workspace_id, created_at_us, job_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX speech_jobs_workspace_created_id")
