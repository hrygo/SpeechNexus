"""Admit the HTTP publication command into the binding lifecycle journal.

Publication is the third binding lifecycle command: it appends one publication
and evidence row, advances the selected head and records the original key in the
same event journal that already stores retire, disable and activate. The journal's
action CHECK excluded ``model_binding.publish`` because only the operator CLI
paused at the publication boundary; fresh schemas carry the widened CHECK through
``LIFECYCLE_DDL`` and this revision rewrites it on an existing database without
touching a stored event row.
"""

from alembic import op
from sqlalchemy import text

revision = "0051_binding_publication_events"
down_revision = "0050_profile_retirements"
branch_labels = None
depends_on = None

_EVENTS = "speech_publication_lifecycle_events"
_CONSTRAINT = _EVENTS + "_action_check"
_CURRENT_ACTIONS = (
    "CHECK (action IN ('model_binding.publish', 'model_binding.retire', "
    "'model_binding.disable', 'model_binding.activate'))"
)
_LEGACY_ACTIONS = (
    "CHECK (action IN ('model_binding.retire', 'model_binding.disable', 'model_binding.activate'))"
)


def upgrade() -> None:
    # Serialize with a concurrent lifecycle writer before replacing the
    # constraint; the rewrite scans the append-only journal once.
    op.execute(f"LOCK TABLE {_EVENTS} IN SHARE ROW EXCLUSIVE MODE")
    op.execute(f"ALTER TABLE {_EVENTS} DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(f"ALTER TABLE {_EVENTS} ADD CONSTRAINT {_CONSTRAINT} {_CURRENT_ACTIONS}")


def downgrade() -> None:
    connection = op.get_bind()
    publication_events = connection.execute(
        text(f"SELECT EXISTS (SELECT 1 FROM {_EVENTS} WHERE action='model_binding.publish')")
    ).scalar_one()
    if publication_events:
        raise RuntimeError("Binding publication events require preservation")
    op.execute(f"LOCK TABLE {_EVENTS} IN SHARE ROW EXCLUSIVE MODE")
    op.execute(f"ALTER TABLE {_EVENTS} DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(f"ALTER TABLE {_EVENTS} ADD CONSTRAINT {_CONSTRAINT} {_LEGACY_ACTIONS}")
