"""Allow pronunciation-set revisions to use the shared publication identity."""

from alembic import op
from sqlalchemy import text

revision = "0018_publication_pronunciation"
down_revision = "0017_job_submissions"
branch_labels = None
depends_on = None

_CONSTRAINT = "speech_publications_kind_check"
_ALLOWED = "('profile', 'binding', 'voice', 'pronunciation')"
_ORIGINAL = "('profile', 'binding', 'voice')"


def upgrade() -> None:
    op.execute(f"ALTER TABLE speech_publications DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE speech_publications ADD CONSTRAINT {_CONSTRAINT} CHECK (kind IN {_ALLOWED})"
    )


def downgrade() -> None:
    connection = op.get_bind()
    op.execute("LOCK TABLE speech_publications IN ACCESS EXCLUSIVE MODE")
    if connection.execute(
        text("SELECT count(*) FROM speech_publications WHERE kind = 'pronunciation'")
    ).scalar_one():
        raise RuntimeError("Pronunciation publications require preservation and recovery")
    op.execute(f"ALTER TABLE speech_publications DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE speech_publications ADD CONSTRAINT {_CONSTRAINT} CHECK (kind IN {_ORIGINAL})"
    )
