"""Record publication evidence without changing publication or revision rows."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    PUBLICATION_EVIDENCE_DDL,
    TABLE,
)

revision = "0021_publication_evidence"
down_revision = "0020_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PUBLICATION_EVIDENCE_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Publication evidence downgrade requires READ COMMITTED isolation")
    # Hold the lock while checking emptiness so no evidence can appear after it.
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    if connection.execute(text("SELECT count(*) FROM " + TABLE)).scalar_one():
        raise RuntimeError("Publication evidence requires export and preservation")
    op.execute("DROP TABLE " + TABLE)
