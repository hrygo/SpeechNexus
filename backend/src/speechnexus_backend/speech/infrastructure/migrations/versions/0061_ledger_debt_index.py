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
This revision therefore indexes the ledger's unsettled rows --
``settled_at_us IS NULL OR last_outcome = 'exhausted'`` -- as the first attempt at a
bound.

**That bound does not hold, and migration 0062 supersedes this index.** An
``exhausted`` row keeps ``settled_at_us`` NULL forever: nothing in the codebase ever
settles it, deliberately, because the row is deletion evidence. Such a row is
therefore a *permanent* member of this partial index even after its lease was
released and the capacity returned, so the index grows with the deployment's refused
releases rather than with the capacity still held, and the read's cost follows it:
measured 0.058 ms with 10 open rows, 0.879 ms with 100 exhausted history rows (the
planner leaves the nested loop for a sequential scan of the fence table), 1.204 ms
with 2 000. 0062 drops this index and indexes the leases that still hold capacity
instead -- a set bounded by the instance slots -- and its docstring carries the
measurements.

The two disjuncts are not redundant today: the table's
``CHECK (settled_at_us IS NULL OR last_outcome = 'settled')`` already makes
``exhausted`` imply an unsettled row. They are both stated so a later relaxation of
that constraint cannot silently drop the rows this read exists for, and so the index
says what the read asks for even if the constraint moves.

``CREATE INDEX`` and not ``CREATE INDEX CONCURRENTLY``: the migration runner
executes every revision inside one transaction (``migrations/env.py`` holds
``connection.begin()``), where the concurrent form is not allowed to run; the plain
build is short -- 4.7 ms measured on a 50 010-row ledger with 10 open rows in this
slice.

The downgrade refuses explicitly: this revision is history now, and a downgrade past
it would run 0062's refusal first. An operator who wants the old cost profile has to
drop the index deliberately.
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
