"""Add explicit publication selection and append-only lifecycle evidence."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.catalog_revision_schema import RETIREMENT_TABLE
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
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
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {RETIREMENT_TABLE} IN ACCESS EXCLUSIVE MODE")
    for statement in LIFECYCLE_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Publication lifecycle downgrade requires READ COMMITTED isolation")
    op.execute(f"LOCK TABLE {HEAD_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {EVENT_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {RETIREMENT_TABLE} IN ACCESS EXCLUSIVE MODE")
    lifecycle_rows = connection.execute(text(f"SELECT count(*) FROM {EVENT_TABLE}")).scalar_one()
    non_retire_rows = connection.execute(
        text(
            f"SELECT count(*) FROM {RETIREMENT_TABLE} "
            "WHERE mode <> 'retire' OR reason IS NOT NULL"
        )
    ).scalar_one()
    if lifecycle_rows or non_retire_rows:
        raise RuntimeError(LIFECYCLE_DOWNGRADE_MESSAGE)
    op.execute(f"DROP TABLE {EVENT_TABLE}")
    op.execute(f"DROP TABLE {HEAD_TABLE}")
    op.execute(f"ALTER TABLE {RETIREMENT_TABLE} DROP CONSTRAINT {RETIREMENT_TABLE}_retire_reason")
    op.execute(f"ALTER TABLE {RETIREMENT_TABLE} DROP CONSTRAINT {RETIREMENT_TABLE}_disable_reason")
    op.execute(f"ALTER TABLE {RETIREMENT_TABLE} DROP COLUMN reason")
    op.execute(f"ALTER TABLE {RETIREMENT_TABLE} DROP COLUMN mode")
