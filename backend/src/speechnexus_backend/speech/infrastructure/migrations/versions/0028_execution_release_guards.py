"""Corroborate native no-dispatch failure before returning lease increments."""

from alembic import op

from speechnexus_backend.speech.infrastructure.execution_release_schema import RELEASE_GUARD_DDL

revision = "0028_execution_release_guards"
down_revision = "0027_gateway_abort_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(RELEASE_GUARD_DDL)


def downgrade() -> None:
    # Source facts and all settlement history remain intact. Application rollout
    # must retain the guard: an older consumer does not enforce this check.
    op.execute("DROP VIEW execution_release_guards")
