"""Retire the Job index superseded by the stable workspace keyset index.

``speech_jobs_workspace_created_id (workspace_id, created_at_us, job_id)``
added by ``0040_public_job_listing`` covers every prefix of
``speech_jobs_workspace_created (workspace_id, created_at_us DESC)``: the only
product query orders by ``created_at_us ASC, job_id ASC`` and plans as a forward
scan of the keyset index, so the older index serves no read while every insert
still maintains a second, near-identical index entry. Keep the merged
migrations unchanged and remove the redundant index here.
"""

from alembic import op

revision = "0042_drop_superseded_job_index"
down_revision = "0041_voice_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX speech_jobs_workspace_created")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX speech_jobs_workspace_created "
        "ON speech_jobs (workspace_id, created_at_us DESC)"
    )
