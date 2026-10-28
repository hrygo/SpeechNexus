"""Add explicit publication selection and append-only lifecycle evidence."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.catalog_revision_schema import RETIREMENT_TABLE
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    DISABLE_INTENT_TABLE,
    EVENT_TABLE,
    HEAD_TABLE,
    LIFECYCLE_DDL,
    LIFECYCLE_DOWNGRADE_MESSAGE,
)
from speechnexus_backend.speech.infrastructure.publication_schema import TABLE

revision = "0023_publication_lifecycle"
down_revision = "0022_catalog_revision_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Publication lifecycle upgrade requires READ COMMITTED isolation")
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {RETIREMENT_TABLE} IN ACCESS EXCLUSIVE MODE")
    for statement in LIFECYCLE_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Publication lifecycle downgrade requires READ COMMITTED isolation")
    # Freeze both versions of visibility before comparing them. Concurrent
    # publishers may be aborted by PostgreSQL's deadlock detector; no writer
    # or migration may report success after losing a committed selection.
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {RETIREMENT_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {HEAD_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {DISABLE_INTENT_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {EVENT_TABLE} IN ACCESS EXCLUSIVE MODE")
    lifecycle_rows = connection.execute(text(f"SELECT count(*) FROM {EVENT_TABLE}")).scalar_one()
    non_retire_rows = connection.execute(
        text(
            f"SELECT count(*) FROM {RETIREMENT_TABLE} WHERE mode <> 'retire' OR reason IS NOT NULL"
        )
    ).scalar_one()
    if lifecycle_rows or non_retire_rows:
        raise RuntimeError(LIFECYCLE_DOWNGRADE_MESSAGE)
    different_visibility = connection.execute(
        text(
            f"WITH legacy AS (SELECT p.kind, p.object_id, p.revision, p.revision_id "
            f"FROM {TABLE} p WHERE NOT EXISTS (SELECT 1 FROM {TABLE} newer "
            "WHERE newer.kind=p.kind AND newer.object_id=p.object_id "
            "AND newer.revision::numeric > p.revision::numeric) "
            f"AND NOT EXISTS (SELECT 1 FROM {RETIREMENT_TABLE} r "
            "WHERE r.kind=p.kind AND r.object_id=p.object_id "
            "AND r.revision=p.revision AND r.revision_id=p.revision_id)), "
            f"current_visibility AS (SELECT h.kind,h.object_id,h.revision,h.revision_id "
            f"FROM {HEAD_TABLE} h JOIN {TABLE} p ON p.kind=h.kind "
            "AND p.object_id=h.object_id AND p.revision=h.revision "
            "AND p.revision_id=h.revision_id "
            f"WHERE NOT EXISTS (SELECT 1 FROM {RETIREMENT_TABLE} r "
            "WHERE r.kind=h.kind AND r.object_id=h.object_id "
            "AND r.revision=h.revision AND r.revision_id=h.revision_id)) "
            "SELECT EXISTS ((SELECT * FROM legacy EXCEPT SELECT * FROM current_visibility) "
            "UNION ALL (SELECT * FROM current_visibility EXCEPT SELECT * FROM legacy))"
        )
    ).scalar_one()
    if different_visibility:
        raise RuntimeError("Publication head visibility requires preservation")
    op.execute(f"DROP TABLE {DISABLE_INTENT_TABLE}")
    op.execute(f"DROP TABLE {EVENT_TABLE}")
    op.execute(f"DROP TABLE {HEAD_TABLE}")
    op.execute(f"ALTER TABLE {RETIREMENT_TABLE} DROP CONSTRAINT {RETIREMENT_TABLE}_retire_reason")
    op.execute(f"ALTER TABLE {RETIREMENT_TABLE} DROP CONSTRAINT {RETIREMENT_TABLE}_disable_reason")
    op.execute(f"ALTER TABLE {RETIREMENT_TABLE} DROP COLUMN reason")
    op.execute(f"ALTER TABLE {RETIREMENT_TABLE} DROP COLUMN mode")
