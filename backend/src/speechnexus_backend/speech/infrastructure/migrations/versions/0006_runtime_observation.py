"""Preserve the trusted gateway/qualification receipt beside actual observations."""

from alembic import op

from speechnexus_backend.speech.infrastructure.runtime_observation_schema import (
    RUNTIME_OBSERVATION_DDL,
)

revision = "0006_runtime_observation"
down_revision = "0005_runtime_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in RUNTIME_OBSERVATION_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Destructive runtime readiness downgrade requires explicit history recovery")
