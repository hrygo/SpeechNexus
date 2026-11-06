"""Atomically attach voice drafts to public generation Jobs; reserve 0041 for #490."""

from alembic import op

from speechnexus_backend.speech.infrastructure.voice_draft_schema import VOICE_DRAFT_DDL

revision = "0042_voice_drafts"
down_revision = "0040_public_job_listing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE speech_jobs DROP CONSTRAINT speech_jobs_kind_check")
    op.execute(
        "ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_kind_check "
        "CHECK (kind IN ('asr', 'tts', 'voice_design', 'voice_clone'))"
    )
    for statement in VOICE_DRAFT_DDL:
        op.execute(statement)


def downgrade() -> None:
    # Restoring the narrower CHECK fails with voice Jobs, preserving their history.
    op.execute("ALTER TABLE speech_jobs DROP CONSTRAINT speech_jobs_kind_check")
    op.execute(
        "ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_kind_check "
        "CHECK (kind IN ('asr', 'tts'))"
    )
    op.execute("DROP TABLE speech_voice_metadata_commands")
    op.execute("DROP TABLE speech_voice_drafts")
