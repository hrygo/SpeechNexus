"""Let the draft reader project the facts of a published draft.

Publication already records the immutable revision and the capability evidence,
but ``getVoiceDraft`` serves the draft from the Job-writer identity, which owns
neither table. Without a projection a published draft is unreadable: its
revision is missing the required ``validation_job_id`` and the published voice
identity, so the response violates the declared 200 and the API answers 500.

The view is owner-privileged (``security_invoker = false``) exactly like the
catalogue reader view: the draft reader may see that a draft became a voice and
which validation Job measured it, and still may not read the evidence ledger or
the immutable revision document. It exposes one row per published draft
revision, so a never-published draft simply has no row.

Grants for the API identity are applied by the migration owner through
``voice_draft_publication_reader_grants``.
"""

from alembic import op

from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    DRAFT_PUBLICATION_VIEW,
    DRAFT_PUBLICATION_VIEW_DDL,
)

revision = "0053_voice_draft_publication"
down_revision = "0052_quarantine_releases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(DRAFT_PUBLICATION_VIEW_DDL)


def downgrade() -> None:
    # The view is a projection, not a copy: the facts stay in the revision and
    # evidence tables, so dropping it loses nothing that has to be preserved.
    op.execute(f"DROP VIEW {DRAFT_PUBLICATION_VIEW}")
