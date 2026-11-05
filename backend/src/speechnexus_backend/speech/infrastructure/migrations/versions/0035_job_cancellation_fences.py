"""Persist the gateway-facing cancellation fence and reconciliation schedule.

The authoritative acceptance fact remains ``speech_job_cancellations`` (0034).
The fence is its transactional, gateway-readable projection: it contains only
``job_id``, ``request_id`` and the acceptance timestamp, so the gateway role
never needs the receipt's operation/attempt/digest columns. Acceptance writes
both rows in one transaction; this migration backfills fences for receipts that
already exist. The gateway reads the fence inside submit; when present it must
not create a runnable execution. Neither table proves a stop or releases
capacity.
"""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    JOB_CANCELLATION_FENCE_DDL,
    NOT_SUBMITTED_TABLE,
    RECONCILIATION_TABLE,
    TABLE,
)

# The public Job cancellation acceptance migration (0034) owns the cancelling
# state and receipt. This migration follows it so the acceptance transaction can
# write the fence in the same transaction. The acceptance must call
# speech.infrastructure.job_cancellation_fence.write_cancellation_fence with its
# own cursor; provisioning the table alone does not create a fence.
revision = "0035_job_cancellation_fences"
down_revision = "0034_public_job_cancellations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in JOB_CANCELLATION_FENCE_DDL:
        op.execute(statement)
    # The receipt is the single acceptance fact; the fence is its projection.
    op.execute(
        f"INSERT INTO {TABLE} (job_id, request_id, accepted_at_us) "
        "SELECT job_id, request_id, requested_at_us FROM speech_job_cancellations "
        "WHERE accepted ON CONFLICT (job_id) DO NOTHING"
    )


def downgrade() -> None:
    connection = op.get_bind()
    # Freeze all three sides; a concurrent acceptance must not appear after the check.
    op.execute(
        f"LOCK TABLE {TABLE}, {NOT_SUBMITTED_TABLE}, {RECONCILIATION_TABLE} "
        "IN ACCESS EXCLUSIVE MODE"
    )
    if connection.execute(text(f"SELECT count(*) FROM {RECONCILIATION_TABLE}")).scalar_one():
        raise RuntimeError("Cancellation reconciliation history requires preservation")
    if connection.execute(text(f"SELECT count(*) FROM {NOT_SUBMITTED_TABLE}")).scalar_one():
        raise RuntimeError("Cancellation not-submitted observations require preservation")
    if connection.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar_one():
        raise RuntimeError("Cancellation fences require preservation")
    op.execute(f"DROP TABLE {NOT_SUBMITTED_TABLE}")
    op.execute(f"DROP TABLE {RECONCILIATION_TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
