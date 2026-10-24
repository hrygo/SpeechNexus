"""Append draft configuration history without exposing a publication writer."""

from alembic import op

from speechnexus_backend.speech.infrastructure.profile_draft_schema import PROFILE_DRAFT_DDL

revision = "0009_profile_drafts"
down_revision = "0008_runtime_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PROFILE_DRAFT_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Profile history downgrade requires explicit preservation and recovery")
