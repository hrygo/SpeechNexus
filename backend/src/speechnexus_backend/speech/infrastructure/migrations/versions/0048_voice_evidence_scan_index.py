"""Index the durable evidence scan predicate and order.

``speechnexus-voice-evidence-worker`` polls ``speech_jobs`` every few seconds for
succeeded ``voice_validation`` Jobs that have no evidence row yet, ordered by
``(updated_at_us, job_id)``. Without an index that pass seq-scans the whole Job
table and top-N sorts it, so the cost grows with every unrelated Job ever
written. The partial index covers both the scan predicate and the order, so one
pass reads only the candidate range.

The predicate is the mutable ``(kind, state)`` pair, matching exactly what the
scan filters before applying the evidence ``NOT EXISTS``. ``result_json`` is not
part of the index: a malformed historical row must remain a candidate that is
refused with a stable reason, not an expression that could make the index or the
scan fail.
"""

from alembic import op

revision = "0048_voice_evidence_scan_index"
down_revision = "0047_voice_publication_evidence"
branch_labels = None
depends_on = None

_INDEX = "speech_jobs_voice_evidence_scan"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX {_INDEX} ON speech_jobs (updated_at_us, job_id) "
        "WHERE kind = 'voice_validation' AND state = 'succeeded'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX {_INDEX}")
