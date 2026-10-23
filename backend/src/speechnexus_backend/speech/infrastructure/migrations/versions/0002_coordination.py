"""Complete the shared storage foundation without rewriting Job/outbox history."""

from alembic import op

from speechnexus_backend.speech.infrastructure.coordination_schema import COORDINATION_DDL

revision = "0002_coordination"
down_revision = "0001_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in COORDINATION_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Destructive coordination downgrade requires an explicit recovery plan")
