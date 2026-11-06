"""Expose only existing published voice documents to the catalogue reader."""

from alembic import op

from speechnexus_backend.speech.infrastructure.voice_catalogue_schema import (
    VOICE_READER_VIEW,
    VOICE_READER_VIEW_DDL,
)

revision = "0037_voice_catalogue_reader"
down_revision = "0036_abort_dispatch_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(VOICE_READER_VIEW_DDL)


def downgrade() -> None:
    op.execute(f"DROP VIEW {VOICE_READER_VIEW}")
