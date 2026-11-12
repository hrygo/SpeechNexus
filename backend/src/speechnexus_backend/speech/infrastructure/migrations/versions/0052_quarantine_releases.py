"""Record operator-verified quarantine releases exactly once.

``releaseQuarantinedAdmissionLease`` needs a durable landing place for the
operator evidence, the server release time and the idempotency-key digest. The
lease row cannot carry them: its columns are shared by five writers, its
mutability boundary differs from an immutable event, and the key scope is
"deployment + lease" rather than "one column on a hot row". This revision adds
the append-only ledger and the trigger that makes "evidence is never rewritten"
a database guarantee. Runtime grants for the dedicated release role are applied
separately by the migration owner; see ``quarantine_release_grants``.
"""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    QUARANTINE_RELEASE_DDL,
    QUARANTINE_RELEASE_TRIGGER_DDL,
    TABLE,
    TRIGGER,
)

revision = "0052_quarantine_releases"
down_revision = "0051_binding_publication_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(QUARANTINE_RELEASE_DDL)
    for statement in QUARANTINE_RELEASE_TRIGGER_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    # Freeze the ledger before deciding whether the older shape can represent
    # the committed facts; a concurrent release must not appear after the check.
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    recorded = connection.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar_one()
    if recorded:
        raise RuntimeError("Quarantine release evidence requires preservation")
    op.execute(f"DROP TRIGGER {TRIGGER} ON {TABLE}")
    op.execute(f"DROP FUNCTION {TABLE}_reject_mutation()")
    op.execute(f"DROP TABLE {TABLE}")
