"""Publish prepared revisions explicitly; the catalog only reads these records."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.publication_schema import (
    PUBLICATION_DDL,
    TABLE,
)

revision = "0015_publications"
down_revision = "0014_pronunciation_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PUBLICATION_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Publication downgrade requires READ COMMITTED isolation")
    # Hold the lock while checking emptiness so no publication can appear after it.
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    if connection.execute(text("SELECT count(*) FROM " + TABLE)).scalar_one():
        raise RuntimeError("Published revisions require preservation and recovery")
    op.execute("DROP TABLE " + TABLE)
