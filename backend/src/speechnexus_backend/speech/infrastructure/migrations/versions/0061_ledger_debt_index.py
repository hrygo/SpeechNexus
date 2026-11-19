"""Index the cancellation ledger's open rows so the permanent debt read is bounded.

``cancellation_debt`` gains a fifth read (#1008): an accepted cancellation whose
ledger row is unsettled or permanently un-claimable (``last_outcome = 'exhausted'``)
while one of its leases still holds capacity, read **without** a lookback window. That
shape left every earlier read face -- the two state-gated reads require
``state = 'cancelling'``, and the fourth read is bounded by a 24 h acceptance
window -- so a Job that closed as ``cancelled`` while its release was refused and
whose ledger was later recorded as ``exhausted`` held capacity where nothing polled
would ever name it again.

The read cannot be bounded by acceptance age (that is the point: the debt is the
oldest debt), and it cannot walk ``speech_job_cancellation_reconciliations`` either:
that table keeps one row per accepted cancellation for the deployment's whole life.
This partial index is the bound instead. It holds only the rows that can still
describe held capacity -- ``settled_at_us IS NULL`` or ``last_outcome =
'exhausted'`` -- so its size follows the open debt rather than the cancellation
history, and the 5 s poll's cost follows the index, not uptime.

The two disjuncts are not redundant today: the table's
``CHECK (settled_at_us IS NULL OR last_outcome = 'settled')`` already makes
``exhausted`` imply an unsettled row. They are both stated so a later relaxation of
that constraint cannot silently drop the rows this read exists for, and so the index
says what the read asks for even if the constraint moves.

``CREATE INDEX`` and not ``CREATE INDEX CONCURRENTLY``: the migration runner
executes every revision inside one transaction (``migrations/env.py`` holds
``connection.begin()``), where the concurrent form is not allowed to run; the plain
build is short because the index only holds the open rows -- 4.7 ms measured on a
50 010-row ledger with 10 open rows in this slice.

The downgrade refuses explicitly. Dropping the index would silently return the
polled read to a whole-ledger scan and re-hide the permanently held capacity class
that no other read can name, which is a runtime safety property rather than stale
data. An operator who really wants the old cost profile has to drop the index
deliberately.
"""

from alembic import op

revision = "0061_ledger_debt_index"
down_revision = "0060_left_cancelling_debt_index"
branch_labels = None
depends_on = None

_INDEX = "speech_job_cancellation_ledger_debt_scan"
#: "This ledger row can still describe held capacity." Mirrors the read's own
#: predicate so the planner can prove the query implies the index.
_PREDICATE = "settled_at_us IS NULL OR last_outcome = 'exhausted'"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX {_INDEX} ON speech_job_cancellation_reconciliations "
        f"(job_id) WHERE {_PREDICATE}"
    )


def downgrade() -> None:
    raise RuntimeError(
        "0061's speech_job_cancellation_ledger_debt_scan partial index backs the polled "
        "permanent-cancellation debt read: dropping it would return that read to a scan of "
        "speech_job_cancellation_reconciliations whose cost grows with the deployment's "
        "whole cancellation history while the 5 s readiness poll runs, and would re-hide "
        "capacity held by a cancelled Job whose ledger can never be claimed again; the "
        "index stores no business data, so an operator must confirm its removal explicitly"
    )
