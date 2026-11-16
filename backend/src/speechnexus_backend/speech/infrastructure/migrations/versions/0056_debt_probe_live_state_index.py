"""Index the live Job states the two readiness debt probes read.

``/readyz`` runs ``stop_evidence_debt`` and ``cancellation_debt`` on every poll (720/h at
the 5 s deployment interval) and both filter ``speech_jobs`` on a state a Job only holds
while it is live:

* ``stop_evidence_debt`` scans ``state = 'running'`` jobs ordered by ``updated_at_us``;
* ``cancellation_debt`` reaches the same table from ``state = 'cancelling'``.

Without this index the planner has no access path for either predicate, so both queries
fall through to ordinal access and their cost is a function of things that grow for
reasons unrelated to the debt:

* a sequential scan of ``speech_jobs`` (the whole Job history). Reached once the held-lease
  backlog is large enough that the lease-driven nested loop stops being cheaper: measured
  37.8 ms at 1 000 000 Jobs with 20 000 held leases, versus 0.6 ms with 64 -- the same query,
  the same 1 debt row, only the unrelated backlog differed.
* a sequential scan of ``speech_job_cancellation_fences``. That table keeps one row per
  accepted cancellation for the deployment's whole life (nothing prunes it), so the scan
  grows with uptime, not with debt: measured 0.4 ms / 6.3 ms / 43.6 ms at 1 000 / 20 000 /
  200 000 historical cancellations, on a 1 000 000-Job table with a healthy 64-lease live set.

The index covers both predicates. It is partial so it holds only rows in a live state --
16 kB with a 96-row live set and 1 MB with a 20 000-row backlog at 1 000 000 Jobs -- which
keeps the write cost on the two transitions that matter (into ``running``/``cancelling``
and out of them) and leaves the Job history out of it. ``updated_at_us`` follows ``state``
so ``stop_evidence_debt``'s ``ORDER BY updated_at_us, job_id`` can read a range of live rows
in its own order instead of sorting them, and ``job_id`` closes the ordering key.

Measured after the index, same data, same parameters (``grace_us`` = 300 s, ``limit`` = 96):
``cancellation_debt`` 43.6 ms -> 0.47 ms on the long-history shape, and the jobs-scan fallback
of ``stop_evidence_debt`` (37.8 ms) becomes an index scan (33.9 ms). The residual cost of both
queries is proportional to the number of leases still holding capacity, which is the debt
itself: 0.5 ms at 64 held leases, 34 ms at 20 000, 101 ms at 50 000. That is the shape a
deployment is in when the probe's own detail has already named tens of thousands of Jobs.

``CREATE INDEX`` and not ``CREATE INDEX CONCURRENTLY``: the migration runner executes every
revision inside one transaction (``migrations/env.py`` holds ``connection.begin()``), where
the concurrent form is not allowed to run, and the plain build is short -- 37-49 ms measured
on the 1 000 000-Job table. The build takes a ``SHARE`` lock, so it blocks writes for that
window while reads continue, and it runs as one step of the migration job that already owns
the schema.
"""

from alembic import op

revision = "0056_debt_probe_live_state_index"
down_revision = "0055_operator_attestation"
branch_labels = None
depends_on = None

_INDEX = "speech_jobs_live_state_scan"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX {_INDEX} ON speech_jobs (state, updated_at_us, job_id) "
        "WHERE state IN ('running', 'cancelling')"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX {_INDEX}")
