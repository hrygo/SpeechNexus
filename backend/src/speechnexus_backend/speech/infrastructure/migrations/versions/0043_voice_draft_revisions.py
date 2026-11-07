"""Persist immutable voice draft revisions and admit validation Jobs."""

from alembic import op

from speechnexus_backend.speech.infrastructure.voice_draft_schema import (
    VOICE_DRAFT_REVISION_BACKFILL,
    VOICE_DRAFT_REVISION_DDL,
)

revision = "0043_voice_draft_revisions"
down_revision = "0042_drop_superseded_job_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in VOICE_DRAFT_REVISION_DDL:
        op.execute(statement)
    op.execute(VOICE_DRAFT_REVISION_BACKFILL)
    missing = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT count(*) FROM speech_voice_drafts d "
            "LEFT JOIN speech_voice_draft_revisions r "
            "ON r.draft_id=d.draft_id AND r.revision=d.head_revision "
            "WHERE r.draft_id IS NULL"
        )
        .scalar_one()
    )
    if missing:
        raise RuntimeError("voice draft head missing after revision backfill")
    op.execute("ALTER TABLE speech_jobs DROP CONSTRAINT speech_jobs_kind_check")
    op.execute(
        "ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_kind_check "
        "CHECK (kind IN ('asr', 'tts', 'voice_design', 'voice_clone', 'voice_validation'))"
    )


def downgrade() -> None:
    # Install the narrower CHECK before dropping the revision table: validation
    # Jobs make the transaction abort and leave 0043 fully applied.
    op.execute("ALTER TABLE speech_jobs DROP CONSTRAINT speech_jobs_kind_check")
    op.execute(
        "ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_kind_check "
        "CHECK (kind IN ('asr', 'tts', 'voice_design', 'voice_clone'))"
    )
    op.execute("DROP TABLE speech_voice_draft_revisions")
