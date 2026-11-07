"""Measured capability evidence, immutable voice revisions and publication receipts.

``cloned`` draft publications are the only source this migration admits. The
public ``VoiceValidation`` schema is unchanged: capabilities live in the internal
evidence ledger and are re-derived from there by the catalogue reader.

The downgrade refuses while custom publications or recorded evidence exist,
because those rows are the only copy of facts that cannot be reconstructed from
the builtin qualification history.
"""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    EVIDENCE_DDL,
    EVIDENCE_TABLE,
    INTENT_DDL,
    INTENT_TABLE,
    LEGACY_READER_VIEW_DDL,
    READER_VIEW_DDL,
    REVISION_DDL,
    REVISION_TABLE,
)

revision = "0047_voice_publication_evidence"
down_revision = "0046_public_job_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in (*EVIDENCE_DDL, *REVISION_DDL, *INTENT_DDL, READER_VIEW_DDL):
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Voice publication downgrade requires READ COMMITTED isolation")
    # Hold the lock while checking emptiness so no publication can appear after it.
    op.execute(f"LOCK TABLE {REVISION_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {EVIDENCE_TABLE} IN ACCESS EXCLUSIVE MODE")
    custom = connection.execute(text(f"SELECT count(*) FROM {REVISION_TABLE}")).scalar_one()
    measured = connection.execute(text(f"SELECT count(*) FROM {EVIDENCE_TABLE}")).scalar_one()
    if custom or measured:
        raise RuntimeError("Custom voice publications and capability evidence require preservation")
    # Replace in place so the catalogue reader's SELECT grant survives rollback.
    op.execute(LEGACY_READER_VIEW_DDL)
    op.execute(f"DROP TABLE {INTENT_TABLE}")
    op.execute(f"DROP TABLE {REVISION_TABLE}")
    op.execute(f"DROP TABLE {EVIDENCE_TABLE}")
