"""Register committed public Artifacts without exposing private object identities."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.artifact_schema import ARTIFACT_DDL, ARTIFACTS

revision = "0020_artifacts"
down_revision = "0019_publication_reader_view"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in ARTIFACT_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Artifact downgrade requires READ COMMITTED isolation")
    op.execute(f"LOCK TABLE {ARTIFACTS} IN ACCESS EXCLUSIVE MODE")
    if connection.execute(text(f"SELECT count(*) FROM {ARTIFACTS}")).scalar_one():
        raise RuntimeError("Committed Artifacts require preservation and recovery")
    op.execute(f"DROP TABLE {ARTIFACTS}")
