"""Add the derived summary columns read by the pronunciation-set list.

The collection list must answer ``encoding`` and ``entry_count`` without parsing
``definition_json`` per page. Both facts already live in the validated document,
so this migration copies them once for existing history, refuses to continue when
a row cannot be represented, and then makes the copies mandatory with the wire
bounds. ``0014_pronunciation_drafts`` stays the historical table creation; this is
an additive follow-up for installs that already hold revisions.

The downgrade drops only the derived columns. ``encoding`` and ``entry_count`` are
a pure function of ``definition_json``, so dropping them loses no history and a
later upgrade re-derives the same values.
"""

from alembic import op

revision = "0045_pronunciation_set_summary"
down_revision = "0044_voice_preview_jobs"
branch_labels = None
depends_on = None

_TABLE = "speech_pronunciation_revisions"
_ENCODING_CHECK = f"{_TABLE}_encoding_check"
_ENTRY_COUNT_CHECK = f"{_TABLE}_entry_count_check"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN encoding VARCHAR(8)")
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN entry_count INTEGER")
    # Name the migration when the backfill itself cannot parse a row: invalid
    # JSON or a non-array ``entries`` surfaces as ``refusing to constrain``
    # instead of a bare DataError from the cast or the array-length call.
    op.execute(
        f"""
        DO $$
        BEGIN
            UPDATE {_TABLE} SET
                encoding = definition_json::jsonb ->> 'encoding',
                entry_count = jsonb_array_length(definition_json::jsonb -> 'entries');
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION
                'refusing to constrain {_TABLE}: cannot derive encoding/entry_count: %',
                SQLERRM;
        END $$;
        """
    )
    # Fail before SET NOT NULL so an unrepresentable historical row names the
    # migration instead of surfacing as a bare constraint violation.
    unrepresentable = (
        op.get_bind()
        .exec_driver_sql(
            f"SELECT count(*) FROM {_TABLE} WHERE encoding IS NULL "
            "OR encoding NOT IN ('pinyin','ipa') OR entry_count IS NULL "
            "OR entry_count NOT BETWEEN 1 AND 10000"
        )
        .scalar_one()
    )
    if unrepresentable:
        raise RuntimeError(
            f"refusing to constrain {_TABLE}: {unrepresentable} revisions have no "
            "representable derived encoding/entry_count"
        )
    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN encoding SET NOT NULL")
    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN entry_count SET NOT NULL")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_ENCODING_CHECK} "
        "CHECK (encoding IN ('pinyin','ipa'))"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_ENTRY_COUNT_CHECK} "
        "CHECK (entry_count BETWEEN 1 AND 10000)"
    )


def downgrade() -> None:
    # Derived columns only: the authoritative definition_json is untouched, so
    # the values are recomputed by a later upgrade rather than preserved here.
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_ENTRY_COUNT_CHECK}")
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_ENCODING_CHECK}")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS entry_count")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS encoding")
