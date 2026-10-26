"""Allocate immutable pronunciation drafts without publishing or executing them."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.pronunciation_draft_schema import (
    PRONUNCIATION_DRAFT_DDL,
)

revision = "0014_pronunciation_drafts"
down_revision = "0013_profile_lifecycle_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PRONUNCIATION_DRAFT_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Pronunciation downgrade requires READ COMMITTED isolation")
    # Match writer lock order, exclude writes before any emptiness snapshot,
    # and retain the locks until all DDL is committed or rolled back.
    op.execute(
        "LOCK TABLE speech_pronunciation_commands, speech_pronunciation_families, "
        "speech_pronunciation_revisions IN ACCESS EXCLUSIVE MODE"
    )
    for table in (
        "speech_pronunciation_commands",
        "speech_pronunciation_families",
        "speech_pronunciation_revisions",
    ):
        if connection.execute(text("SELECT count(*) FROM " + table)).scalar_one():
            raise RuntimeError("Pronunciation history requires preservation and recovery")
    for table in (
        "speech_pronunciation_commands",
        "speech_pronunciation_revisions",
        "speech_pronunciation_families",
    ):
        op.execute("DROP TABLE " + table)
