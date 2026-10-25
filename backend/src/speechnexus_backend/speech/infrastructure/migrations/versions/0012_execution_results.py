"""Register private results without backfilling legacy keys or changing budgets."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.execution_result_schema import EXECUTION_RESULT_DDL

revision = "0012_execution_results"
down_revision = "0011_binding_parameter_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in EXECUTION_RESULT_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Result downgrade requires READ COMMITTED isolation")
    op.execute("LOCK TABLE submit_intents IN ACCESS EXCLUSIVE MODE")
    if connection.execute(
        text("SELECT count(*) FROM submit_intents WHERE staging_result_document IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("Result history downgrade requires preservation and recovery")
    op.execute("ALTER TABLE submit_intents DROP CONSTRAINT execution_result_shape")
    op.execute("ALTER TABLE submit_intents DROP COLUMN staging_result_document")
