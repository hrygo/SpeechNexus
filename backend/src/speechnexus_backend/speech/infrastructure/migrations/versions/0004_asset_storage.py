"""Record an immutable storage authority before any object publication/cleanup."""

from alembic import op

from speechnexus_backend.speech.infrastructure.asset_storage_schema import ASSET_STORAGE_DDL

revision = "0004_asset_storage"
down_revision = "0003_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in ASSET_STORAGE_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Destructive asset downgrade requires explicit namespace recovery")
