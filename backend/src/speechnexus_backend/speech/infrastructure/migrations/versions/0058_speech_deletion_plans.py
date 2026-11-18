"""Immutable speech deletion plans: the preview half of the deletion contract.

A plan pins the exact target revision, content digest and dependency closure read
under one lock. It expires after the contract's fixed window and authorizes
nothing: acceptance, stop coordination and the durable ledger are a later slice.
The private ``content_snapshot`` carries the owner metadata execution rechecks;
the public ``dependencies`` array is what ``plan_digest`` is computed over.

Without this table the route would have to answer the frozen placeholder 503
forever. The migration adds storage only; it does not enable any deletion.
"""

from alembic import op

from speechnexus_backend.speech.infrastructure.deletion_plan_schema import (
    DELETION_PLAN_DDL,
)

revision = "0058_speech_deletion_plans"
down_revision = "0057_asr_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in DELETION_PLAN_DDL:
        op.execute(statement)


def downgrade() -> None:
    # An issued plan is a credential: the operator executes a deletion by this
    # plan_id and plan_digest, and a same-key retry would collide with the lost
    # row. The 300-second TTL bounds exposure, not the consequence of losing it,
    # so a downgrade must never discard accepted rows on its own.
    raise RuntimeError(
        "Deletion plan history requires preservation: issued preview rows are "
        "plan_id/plan_digest deletion credentials, dropping them loses their replay, "
        "and a same-key retry would collide with the lost rows; an operator must "
        "confirm and handle the rows explicitly"
    )
