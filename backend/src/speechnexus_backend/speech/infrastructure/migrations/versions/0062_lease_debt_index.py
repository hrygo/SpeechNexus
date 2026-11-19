"""Index the leases that still hold capacity, so the permanent debt read is bounded.

``cancellation_debt`` gains a fifth read (#1008): an accepted cancellation whose lease
still holds capacity while no bounded read names it any more -- the acceptance aged out
of the fourth read's 24 h window, the worker never claimed it so there is no ledger row,
or the ledger was recorded ``exhausted``. That read must not be bounded by time (the
point is that the debt is the oldest debt), so the bound has to be something that cannot
grow with the deployment's history.

Migration 0061 tried the ledger first: a partial index over
``settled_at_us IS NULL OR last_outcome = 'exhausted'``. That predicate does not bound
anything, because an ``exhausted`` row is a **permanent** member of it -- nothing in the
codebase ever settles such a row, by design (it is deletion evidence) -- even after the
lease behind it was released and the capacity returned. Measured on the same data the
read is flat with 10 open rows (0.058 ms) and then degrades with the *failure history*:
0.879 ms at 100 exhausted rows (the planner abandons the nested loop and sequentially
scans the fence table), 1.204 ms at 2 000. So 0062 drops that index (this file owns the
supersession) and indexes the set that is genuinely bounded instead.

A lease that still holds capacity is a live reservation, and reservations cannot exceed
the deployment's instance slots. This index therefore holds a number bounded by capacity
-- not by how many cancellations were ever accepted, how many ledger rows were ever
written, or how long the deployment has been up. It is partial for the same reason
migration 0056's live-state index is: ``admission_leases`` keeps every lease forever
(released and quarantined rows included), and those are exactly the rows a debt read never
wants. Measured with the three histories scaled 10x together (settled ledger rows,
exhausted ledger rows, released leases): 0.06-0.08 ms, 132 buffers, 10 rows, and the plan
is an index scan on this index followed by primary-key lookups -- no sequential scan of
``admission_leases``, ``speech_jobs``, ``speech_job_cancellation_fences`` or
``speech_job_cancellation_reconciliations``.

The read states the predicate exactly as stored here
(``l.lease_state NOT IN ('released', 'quarantined')``) so the planner can prove the
query implies the index, and ``operation_id`` carries the join to the Job, the fence and
the ledger.

``CREATE INDEX`` and not ``CREATE INDEX CONCURRENTLY``: the migration runner executes
every revision inside one transaction (``migrations/env.py`` holds ``connection.begin()``),
where the concurrent form is not allowed to run; the plain build is short because the
partial predicate excludes the lease history -- 1.9 ms measured on a 20 010-row lease
table with 10 held rows in this slice.

The downgrade refuses explicitly. Dropping this index would return the polled read to a
sequential scan of ``admission_leases`` whose cost grows with the deployment's whole lease
history while the 5 s readiness poll runs, and would re-hide the capacity class that no
other read can name; it also cannot restore 0061's ledger index, which this revision
dropped because it does not bound the read. An operator who really wants the old cost
profile has to rebuild it deliberately.
"""

from alembic import op

revision = "0062_lease_debt_index"
down_revision = "0061_ledger_debt_index"
branch_labels = None
depends_on = None

_INDEX = "speech_job_cancellation_lease_debt_scan"
#: The index 0061 added. It is dropped here, not left behind: its predicate keeps every
#: ``exhausted`` row forever, so it grows with the deployment's failures while no read
#: uses it any more -- an index that only costs writes. See this module's docstring for
#: the measurements that retired it.
_SUPERSEDED = "speech_job_cancellation_ledger_debt_scan"
#: "This lease still holds capacity." Mirrors the read's own predicate so the planner can
#: prove the query implies the index.
_PREDICATE = "lease_state NOT IN ('released', 'quarantined')"


def upgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_SUPERSEDED}")
    op.execute(f"CREATE INDEX {_INDEX} ON admission_leases (operation_id) WHERE {_PREDICATE}")


def downgrade() -> None:
    raise RuntimeError(
        "0062's speech_job_cancellation_lease_debt_scan partial index is the access path "
        "of the polled permanent-cancellation debt read: dropping it would return that "
        "read to a scan of admission_leases whose cost grows with the deployment's whole "
        "lease history while the 5 s readiness poll runs, and would re-hide capacity held "
        "by an accepted cancellation that no bounded read names any more; the index stores "
        "no business data, and the ledger index it replaced (0061's "
        "speech_job_cancellation_ledger_debt_scan) was dropped by this revision because it "
        "does not bound the read, so an operator must confirm its removal explicitly"
    )
