"""Atomic profile publication/current heads and immutable original results."""

from alembic import op

from speechnexus_backend.speech.infrastructure.profile_publication_schema import (
    PROFILE_PUBLICATION_DDL,
)

revision = "0032_profile_publications"
down_revision = "0031_tts_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PROFILE_PUBLICATION_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Profile publication history requires preservation and forward recovery")
