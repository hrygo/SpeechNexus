"""Index the fence acceptance age for the Job-left-cancelling debt read.

``cancellation_debt`` gains a fourth read (#852 residual): an accepted cancellation
whose ledger is unsettled and whose Job already *left* ``cancelling`` while a lease
still holds capacity. That read cannot use ``speech_jobs_live_state_scan`` from
migration 0056 -- the partial index holds only ``running`` and ``cancelling`` Jobs,
which is precisely the state this shape has left -- and it cannot scan
``speech_job_cancellation_fences`` either: that table keeps one row per accepted
cancellation for the deployment's whole life (nothing prunes it), so its cost would
grow with uptime while the probe polls every 5 s.

The read is bounded by acceptance age instead: it only asks for fences accepted in
the last ``left_cancelling_window_seconds`` (24 h by default, always larger than the
probe's ``grace_seconds``). This index makes that predicate a range on
``accepted_at_us`` with ``job_id`` available for the join and the
``accepted_at_us, job_id`` ordering, so the work follows the acceptances inside the
window rather than the settled history before it. Debt older than the window is not
lost: the reconciliation worker's claim has no lower age bound (it keeps claiming old
unsettled fences while the ledger is neither settled nor exhausted), and the operator
drill-down query in ``docs/components/production-jobs.md`` is unbounded. The index is
cheap -- it holds no business data, only the fence key and its acceptance instant --
and the write cost stays on the cancellation acceptance path.

``CREATE INDEX`` and not ``CREATE INDEX CONCURRENTLY``: the migration runner executes
every revision inside one transaction (``migrations/env.py`` holds
``connection.begin()``), where the concurrent form is not allowed to run; the plain
build is short -- 46.8 ms measured on a 50 000-row fence table with this slice.

The downgrade refuses explicitly. Dropping the index would silently return the
polled read to a whole-history scan and re-hide the Job-left-cancelling capacity
class, which is a runtime safety property, not stale data. An operator who really
wants the old cost profile has to drop the index deliberately.
"""

from alembic import op

revision = "0060_left_cancelling_debt_index"
down_revision = "0059_speech_deletion_ledger"
branch_labels = None
depends_on = None

_INDEX = "speech_job_cancellation_fence_age_scan"


def upgrade() -> None:
    op.execute(f"CREATE INDEX {_INDEX} ON speech_job_cancellation_fences (accepted_at_us, job_id)")


def downgrade() -> None:
    raise RuntimeError(
        "0060's speech_job_cancellation_fence_age_scan index backs the polled "
        "left-cancelling debt read: dropping it would return that read to a scan of "
        "speech_job_cancellation_fences whose cost grows with the deployment's "
        "cancellation history while the 5 s readiness poll runs, and would re-hide "
        "capacity held after a Job left cancelling; the index stores no business "
        "data, so an operator must confirm its removal explicitly"
    )
