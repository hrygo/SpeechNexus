"""Append private binding drafts without publication, runtime or default pointers."""

from alembic import op

from speechnexus_backend.speech.infrastructure.binding_draft_schema import BINDING_DRAFT_DDL

revision = "0010_model_binding_drafts"
down_revision = "0009_profile_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in BINDING_DRAFT_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Binding history downgrade requires explicit preservation and recovery")
