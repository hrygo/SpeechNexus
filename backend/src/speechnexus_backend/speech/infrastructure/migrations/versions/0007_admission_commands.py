"""Retain private original operation commands and complete admission envelopes."""

from alembic import op

from speechnexus_backend.speech.infrastructure.admission_command_schema import (
    ADMISSION_COMMAND_DDL,
)

revision = "0007_admission_commands"
down_revision = "0006_runtime_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in ADMISSION_COMMAND_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Destructive admission command downgrade requires explicit history recovery")
