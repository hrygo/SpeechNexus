"""Preserve private original commands without publishing or updating pointers."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.profile_lifecycle_intent_schema import (
    PROFILE_LIFECYCLE_INTENT_DDL,
)

revision = "0013_profile_lifecycle_intents"
down_revision = "0012_execution_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PROFILE_LIFECYCLE_INTENT_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Profile intent downgrade requires READ COMMITTED isolation")
    # Exclude ordinary writers before checking emptiness; hold through DDL.
    op.execute("LOCK TABLE speech_profile_lifecycle_intents IN ACCESS EXCLUSIVE MODE")
    if connection.execute(
        text("SELECT count(*) FROM speech_profile_lifecycle_intents")
    ).scalar_one():
        raise RuntimeError("Profile lifecycle intent history requires preservation and recovery")
    op.execute("DROP TABLE speech_profile_lifecycle_intents")
