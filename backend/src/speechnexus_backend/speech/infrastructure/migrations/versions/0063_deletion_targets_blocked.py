"""Deletion ledger: artifact / voice_revision targets and the blocked reason.

0059 recorded only ``asset`` and ``operation_content`` executions and only the
``queued`` / ``succeeded`` statuses. The confirmed-deletion route now also owns a
committed Artifact and one voice draft revision, and a target that is still
referenced is answered as a Deletion with ``status=blocked`` and the public
ErrorBody explaining why. That reason has to be durable for the same replay and
read guarantees every other column already has, so it is a ledger column rather
than a log line.

The migration widens two value CHECKs and adds one nullable column; it does not
rewrite existing rows, drop evidence or delete content. ``downgrade`` refuses for
the same reason 0059 does: a downgrade would discard recorded blocked/failed
outcomes and let their plans be replayed as fresh deletions.
"""

from alembic import op

from speechnexus_backend.speech.infrastructure.deletion_ledger_schema import (
    DELETION_LEDGER_TARGETS_DDL,
)

revision = "0063_deletion_targets_blocked"
down_revision = "0062_lease_debt_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in DELETION_LEDGER_TARGETS_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError(
        "0063 deletion ledger history requires preservation: rows record completed, "
        "queued, blocked and failed deletions and their replay identity; dropping "
        "the reason or narrowing the recorded target kinds would let a restore "
        "replay an old plan as a new deletion; an operator must confirm and handle "
        "the rows explicitly"
    )
