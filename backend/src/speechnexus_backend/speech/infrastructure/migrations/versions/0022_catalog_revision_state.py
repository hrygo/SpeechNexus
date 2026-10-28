"""Advance one transactional catalogue version for every visible-set change."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.catalog_revision_schema import (
    ADVANCE_FUNCTION,
    CATALOG_REVISION_DDL,
    PUBLICATION_REVISION_IDENTITY,
    PUBLICATION_TRIGGER,
    RETIREMENT_TABLE,
    RETIREMENT_TRIGGER,
    RETIREMENTS_DOWNGRADE_MESSAGE,
    REVISION_DOWNGRADE_MESSAGE,
    STATE_TABLE,
)
from speechnexus_backend.speech.infrastructure.publication_schema import TABLE

revision = "0022_catalog_revision_state"
down_revision = "0021_publication_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old writers must not insert a publication between the seed count and the
    # trigger installation, or the new counter would start below what readers
    # already observed as COUNT(*) + 1.
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    for statement in CATALOG_REVISION_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Catalogue revision downgrade requires READ COMMITTED isolation")
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {RETIREMENT_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {STATE_TABLE} IN ACCESS EXCLUSIVE MODE")
    if connection.execute(text(f"SELECT count(*) FROM {RETIREMENT_TABLE}")).scalar_one():
        raise RuntimeError(RETIREMENTS_DOWNGRADE_MESSAGE)
    legacy_revision = connection.execute(text(f"SELECT count(*) + 1 FROM {TABLE}")).scalar_one()
    stored_revision = connection.execute(
        text(f"SELECT revision FROM {STATE_TABLE} WHERE singleton IS TRUE")
    ).scalar_one_or_none()
    if stored_revision != legacy_revision:
        raise RuntimeError(REVISION_DOWNGRADE_MESSAGE)
    op.execute(f"DROP TRIGGER {PUBLICATION_TRIGGER} ON {TABLE}")
    op.execute(f"DROP TRIGGER {RETIREMENT_TRIGGER} ON {RETIREMENT_TABLE}")
    op.execute(f"DROP FUNCTION {ADVANCE_FUNCTION}()")
    op.execute(f"DROP TABLE {RETIREMENT_TABLE}")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {PUBLICATION_REVISION_IDENTITY}")
    op.execute(f"DROP TABLE {STATE_TABLE}")
