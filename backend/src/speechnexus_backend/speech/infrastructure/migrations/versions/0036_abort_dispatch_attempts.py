"""Replace the one-shot abort dispatch flag with bounded, recoverable attempts.

The original receipt keeps its identity. A dispatch attempt now holds a bounded
lease (attempt identity, start, deadline) and records what the engine answered,
so a crash between claim and send — or between the engine answer and the local
record — leaves a retryable fact instead of a permanent claim. ``dispatched_at_us``
remains the first-attempt instant the read-only recovery projection exposes.
"""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    ABORT_DISPATCH_BACKFILL,
    ABORT_DISPATCH_COLUMNS_DDL,
    ABORT_DISPATCH_CONSTRAINTS_DDL,
    ABORT_INTENT_TABLE,
)

revision = "0036_abort_dispatch_attempts"
down_revision = "0035_job_cancellation_fences"
branch_labels = None
depends_on = None

_DROPPED_CONSTRAINTS = (
    "gateway_abort_instance_generation_check",
    "gateway_abort_dispatch_attempt_check",
    "gateway_abort_dispatch_attempts_check",
)
_DROPPED_COLUMNS = (
    "instance_id",
    "instance_generation",
    "dispatch_attempt_id",
    "dispatch_started_at_us",
    "dispatch_deadline_at_us",
    "dispatch_outcome",
    "dispatch_outcome_at_us",
    "dispatch_attempts",
)


def upgrade() -> None:
    for statement in ABORT_DISPATCH_COLUMNS_DDL:
        op.execute(statement)
    for statement in ABORT_DISPATCH_BACKFILL:
        op.execute(statement)
    for statement in ABORT_DISPATCH_CONSTRAINTS_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Abort dispatch downgrade requires READ COMMITTED isolation")
    op.execute(f"LOCK TABLE {ABORT_INTENT_TABLE} IN ACCESS EXCLUSIVE MODE")
    attempted = connection.execute(
        text(
            f"SELECT EXISTS (SELECT 1 FROM {ABORT_INTENT_TABLE} "
            "WHERE dispatch_attempt_id IS NOT NULL OR dispatch_outcome IS NOT NULL)"
        )
    ).scalar_one()
    if attempted:
        raise RuntimeError("Abort dispatch attempt history requires preservation")
    for constraint in _DROPPED_CONSTRAINTS:
        op.execute(f"ALTER TABLE {ABORT_INTENT_TABLE} DROP CONSTRAINT {constraint}")
    for column in _DROPPED_COLUMNS:
        op.execute(f"ALTER TABLE {ABORT_INTENT_TABLE} DROP COLUMN {column}")
