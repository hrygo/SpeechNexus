"""Expose only published binding revisions to the catalogue reader."""

from alembic import op

from speechnexus_backend.speech.infrastructure.publication_schema import (
    PUBLICATION_READER_VIEW_DDL,
    READER_VIEW,
)

revision = "0019_publication_reader_view"
down_revision = "0018_publication_pronunciation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PUBLICATION_READER_VIEW_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP VIEW {READER_VIEW}")
