"""Store profile retirement intents and the contract's non-empty disable reason.

The publication table already stores the ``retired`` and ``disabled`` terminal
states, and a retirement command is the same original-key ledger entry as
publish/activate. Two CHECK constraints were narrower than the new command:

* the intent journal's operation CHECK excluded ``retire``;
* the publication state CHECK accepted a disabled row with an empty reason
  while the shared ``RetirementRequest`` requires 1-2000 characters.

Fresh schemas are corrected in ``PROFILE_LIFECYCLE_INTENT_DDL`` and
``PROFILE_PUBLICATION_DDL``; this revision rewrites both constraints on an
existing database without touching a stored intent or publication row.
"""

from alembic import op
from sqlalchemy import text

revision = "0050_profile_retirements"
down_revision = "0049_workspace_commands"
branch_labels = None
depends_on = None

_INTENT_CONSTRAINT = "speech_profile_lifecycle_intents_operation_check"
_PUBLICATION_CONSTRAINT = "speech_profile_publications_check"
_PUBLICATION_STATE = (
    "CHECK ((status='published' AND retired_at_us IS NULL AND disabled_reason IS NULL)"
    " OR (status='retired' AND retired_at_us IS NOT NULL"
    " AND retired_at_us >= published_at_us AND disabled_reason IS NULL)"
    " OR (status='disabled' AND disabled_reason IS NOT NULL"
    "{reason}))"
)


def upgrade() -> None:
    op.execute(f"ALTER TABLE speech_profile_lifecycle_intents DROP CONSTRAINT {_INTENT_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE speech_profile_lifecycle_intents ADD CONSTRAINT {_INTENT_CONSTRAINT} "
        "CHECK (operation IN ('publish','activate','retire'))"
    )
    connection = op.get_bind()
    # An empty reason could only come from a writer that bypassed the command
    # boundary. Never rewrite it silently into a valid-looking disable.
    unreasoned = connection.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM speech_profile_publications "
            "WHERE status='disabled' AND length(disabled_reason) NOT BETWEEN 1 AND 2000)"
        )
    ).scalar_one()
    if unreasoned:
        raise RuntimeError("Profile disable reasons require preservation")
    op.execute(f"ALTER TABLE speech_profile_publications DROP CONSTRAINT {_PUBLICATION_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE speech_profile_publications ADD CONSTRAINT {_PUBLICATION_CONSTRAINT} "
        + _PUBLICATION_STATE.format(reason=" AND length(disabled_reason) BETWEEN 1 AND 2000")
    )


def downgrade() -> None:
    connection = op.get_bind()
    pending = connection.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM speech_profile_lifecycle_intents "
            "WHERE operation='retire')"
        )
    ).scalar_one()
    if pending:
        raise RuntimeError("Profile retirement intents require preservation")
    op.execute(f"ALTER TABLE speech_profile_lifecycle_intents DROP CONSTRAINT {_INTENT_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE speech_profile_lifecycle_intents ADD CONSTRAINT {_INTENT_CONSTRAINT} "
        "CHECK (operation IN ('publish','activate'))"
    )
    op.execute(f"ALTER TABLE speech_profile_publications DROP CONSTRAINT {_PUBLICATION_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE speech_profile_publications ADD CONSTRAINT {_PUBLICATION_CONSTRAINT} "
        + _PUBLICATION_STATE.format(reason="")
    )
