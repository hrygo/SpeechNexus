"""Accepted workspace command ledger for same-key replays.

``POST /v1/nexus/workspaces`` and ``PATCH /v1/nexus/workspaces/{workspace_id}``
were declared with an ``Idempotency-Key`` header but had no service. This
revision adds only the ledger those writes need to resolve a retried key before
mutating; it does not add or alter a column of the frozen 0003
``speech_workspaces`` authority and inserts no row.

Replay is enforced by the primary key rather than by read-then-write ordering:
the adapter serialises equal keys with a transaction-scoped advisory lock, then
re-reads a refused duplicate in a fresh transaction because the refused one is
already aborted.

Downgrade drops the ledger. Workspace rows and their metadata revisions are
untouched, but the accepted-intent evidence is lost: after a downgrade a reused
key is no longer recognised and a retry can create a second group. Workspaces
created through the API therefore keep their identity, while exactly-once
replay is only restored by re-applying this revision before accepting writes.
"""

from alembic import op

from speechnexus_backend.speech.infrastructure.workspace_schema import WORKSPACE_COMMAND_DDL

revision = "0049_workspace_commands"
down_revision = "0048_voice_evidence_scan_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in WORKSPACE_COMMAND_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE speech_workspace_commands")
