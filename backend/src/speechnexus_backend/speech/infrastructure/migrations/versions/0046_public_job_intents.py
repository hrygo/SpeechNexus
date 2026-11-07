"""Give the public Job intent its own authoritative storage.

A2 rewrites ``admission_operations.request_document`` into the complete business
command, so the public intent moves to ``speech_job_intents`` first. The create
path keeps writing the legacy copy for readers that A2 has not migrated yet;
reads of the public intent use the new table only.

The backfill accepts a legacy row only when its digest still matches the public
Job digest. A mismatched digest or a redacted (NULL) legacy body is skipped with
a notice: a digest cannot reconstruct the document, and a different operation
must not be presented as this Job's intent.

The downgrade refuses once any intent row has no matching legacy copy, because
dropping the table would then delete the only public intent. It succeeds only
when every intent row is representable in the older shape.
"""

from alembic import op

from speechnexus_backend.speech.infrastructure.public_job_intent_schema import (
    INTENTS,
    JOBS,
    PUBLIC_JOB_INTENT_DDL,
    PUBLIC_JOB_INTENT_DROP,
)

revision = "0046_public_job_intents"
down_revision = "0045_pronunciation_set_summary"
branch_labels = None
depends_on = None

_BACKFILL = f"""
    INSERT INTO {INTENTS} (job_id, request_digest, request_document, recorded_at_us)
    SELECT j.job_id, j.request_digest, a.request_document, GREATEST(1, a.recorded_at_us)
    FROM {JOBS} AS j
    JOIN admission_operations AS a ON a.operation_id = j.job_id
    WHERE a.request_document IS NOT NULL AND a.request_digest = j.request_digest
    ON CONFLICT (job_id) DO NOTHING
"""

_SKIPPED_NOTICE = f"""
    DO $$
    DECLARE skipped BIGINT;
    BEGIN
        SELECT count(*) INTO skipped
        FROM {JOBS} AS j
        JOIN admission_operations AS a ON a.operation_id = j.job_id
        WHERE a.request_document IS NULL OR a.request_digest <> j.request_digest;
        IF skipped > 0 THEN
            RAISE NOTICE 'skipped % legacy rows without a matching public intent', skipped;
        END IF;
    END $$;
"""

_DOWNGRADE_GUARD = f"""
    DO $$
    DECLARE unmatched BIGINT;
    BEGIN
        SELECT count(*) INTO unmatched
        FROM {INTENTS} AS i
        LEFT JOIN admission_operations AS a ON a.operation_id = i.job_id
        WHERE a.request_document IS NULL
           OR a.request_digest IS DISTINCT FROM i.request_digest
           OR a.request_document IS DISTINCT FROM i.request_document;
        IF unmatched > 0 THEN
            RAISE EXCEPTION
                'refusing to drop {INTENTS}: % intent rows have no matching legacy copy',
                unmatched;
        END IF;
    END $$;
"""


def upgrade() -> None:
    for statement in PUBLIC_JOB_INTENT_DDL:
        op.execute(statement)
    op.execute(_BACKFILL)
    op.execute(_SKIPPED_NOTICE)


def downgrade() -> None:
    op.execute(_DOWNGRADE_GUARD)
    for statement in PUBLIC_JOB_INTENT_DROP:
        op.execute(statement)
