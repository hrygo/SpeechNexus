"""Admit ``voice_preview`` Jobs without changing any other schema.

The preview slice submits a pending Job kind that no earlier migration allows:
``speech_jobs_kind_check`` was widened to ``voice_validation`` by
``0043_voice_draft_revisions`` and must now accept ``voice_preview`` too. The
constraint is the only schema fact the new kind needs, so this migration keeps
its blast radius to that one CHECK.

The downgrade restores the five-kind CHECK. A ``voice_preview`` row cannot be
admitted by the narrower constraint, so the downgrade refuses with an explicit
error instead of losing the history of preview Jobs.
"""

from alembic import op

revision = "0044_voice_preview_jobs"
down_revision = "0043_voice_draft_revisions"
branch_labels = None
depends_on = None

_FIVE_KINDS = "'asr', 'tts', 'voice_design', 'voice_clone', 'voice_validation'"
_SIX_KINDS = _FIVE_KINDS + ", 'voice_preview'"


def upgrade() -> None:
    op.execute("ALTER TABLE speech_jobs DROP CONSTRAINT speech_jobs_kind_check")
    op.execute(
        f"ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_kind_check "
        f"CHECK (kind IN ({_SIX_KINDS}))"
    )


def downgrade() -> None:
    # Report the concrete blocker before touching the constraint: the narrower
    # CHECK would reject these rows, and an opaque constraint error is a worse
    # operator message than the count of Jobs that hold the kind.
    count = (
        op.get_bind()
        .exec_driver_sql("SELECT count(*) FROM speech_jobs WHERE kind = 'voice_preview'")
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            f"refusing to narrow speech_jobs_kind_check: {count} voice_preview Job rows still exist"
        )
    op.execute("ALTER TABLE speech_jobs DROP CONSTRAINT speech_jobs_kind_check")
    op.execute(
        f"ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_kind_check "
        f"CHECK (kind IN ({_FIVE_KINDS}))"
    )
