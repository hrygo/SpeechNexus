"""Durable speech deletion ledger: the execution half of the deletion contract.

An issued plan (0058) authorizes nothing. This migration adds the acceptance and
completion record: one row per confirmed deletion, unique per idempotency key and
unique per plan, carrying the exact deleted/retained scope and the digest the
client confirmed. The asset half reuses ``speech_content_deletions`` as its
object-level ledger, so ``reason`` gains the explicit ``confirmed_deletion`` value
instead of recording a confirmed deletion as retention expiry.

Without this table the route can only preview; the migration adds storage, it does
not delete anything by itself. Grants for the Job writer are applied by
``deletion_execution_grants`` after a full upgrade, exactly like 0058's.
"""

from alembic import op

from speechnexus_backend.speech.infrastructure.deletion_ledger_schema import (
    DELETION_LEDGER_DDL,
)

revision = "0059_speech_deletion_ledger"
down_revision = "0058_speech_deletion_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in DELETION_LEDGER_DDL:
        op.execute(statement)


def downgrade() -> None:
    # The ledger is the authoritative record that a deletion happened and what it
    # covered. Dropping it would turn a completed deletion into "never recorded":
    # a restore would re-open content that was already purged, and the plan's key
    # could be replayed as a fresh deletion. Operator-confirmed handling only.
    raise RuntimeError(
        "Deletion ledger history requires preservation: rows are the durable "
        "evidence of completed deletions and their replay identity, dropping them "
        "would let a restore re-open purged content and let an old plan authorize a "
        "new deletion; an operator must confirm and handle the rows explicitly"
    )
