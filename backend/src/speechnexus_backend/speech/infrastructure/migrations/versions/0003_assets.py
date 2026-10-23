"""Shared workspace, asset publication identity and first-resource deletion ledger."""

from alembic import op

from speechnexus_backend.speech.infrastructure.asset_schema import ASSET_DDL

revision = "0003_assets"
down_revision = "0002_coordination"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in ASSET_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Destructive asset downgrade requires an explicit content recovery plan")
