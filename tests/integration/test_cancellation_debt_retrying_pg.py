"""#1038: a permanently refused cancellation debt must not read like a healthy backoff.

``#1036`` fixed the opposite lie: an exhausted acceptance could not be recorded as
``settled`` while its capacity was still held, so the row keeps being retried. That
exposed the mirror problem on the read face. A refused release whose retry never
succeeds produces

    speech_jobs.state                        cancelled
    admission_leases.lease_state             reserved
    speech_job_cancellation_reconciliations  last_outcome = 'stop_unconfirmed',
                                             settled_at_us IS NULL,
                                             attempts = 1 ... 64,
                                             next_check_at_us in the future

and ``/readyz``'s ``cancellation_debt`` detail described it exactly like a worker
about to advance it: ``_SCHEDULE_STATE_SQL`` computed ``scheduled`` and the report
threw that state away, while ``attempts`` -- the only durable sign that the retries
are spent -- was never selected, so ``attempts = 64`` was visible only through the
documented drill-down SQL.

This module drives the production participants against real PostgreSQL: the real
reconciliation ledger and claim predicate, the real ``PublicJobs`` transition and the
real ``LeaseService`` release refusal. The ledger rows are written by the reconciler,
never seeded, so the shape under test is the one the deployment produces. The gateway
HTTP client is a fake; nothing here is engine or GPU evidence.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from tests.integration.test_cancel_failure_slot_pg import (
    LEASE,
    LEASE_SECOND,
    QUARANTINED_DOCUMENT_STATE,
    CancelSandbox,
    _accept_recently,
    _database_now,
    _debt_probe,
    _install_client,
    _reconciler,
    _sandbox,
    _settle_job,
)

from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    JobCancellationReconciler,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    ATTEMPT_CEILING,
    RECONCILIATION_TABLE,
)
from speechnexus_backend.speech.infrastructure.readiness import (
    LEFT_CANCELLING_HOLDING_CAPACITY,
    RETRY_AT_ATTEMPT_CEILING,
)

pytestmark = pytest.mark.integration
SECOND_US = 1_000_000
# The probe judges the schedule on the database clock, so these passes are driven on
# real instants: each one claims exactly when the previous pass said to. The backoff is
# wide enough that a probe running a moment later still sees a *future* claim time
# instead of racing a one-second window -- the assertion is about the state, not about
# how fast the test machine is.
BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 60
# Past the confirmation window (the reconciler's default 300 s), so every claim below
# runs the judgement branch that records ``stop_unconfirmed`` on a refused release.
ACCEPT_AGE_SECONDS = 400


def _retrying_reconciler(box: CancelSandbox) -> JobCancellationReconciler:
    return _reconciler(
        box,
        confirm_timeout_seconds=300,
        max_attempts=3,
        base_backoff_seconds=BACKOFF_SECONDS,
        max_backoff_seconds=MAX_BACKOFF_SECONDS,
    )


def _ledger(box: CancelSandbox) -> dict[str, Any]:
    rows = box.query(
        f"SELECT attempts, last_outcome, settled_at_us, next_check_at_us "
        f"FROM {RECONCILIATION_TABLE} WHERE job_id = %s",
        (box.job_id,),
    )
    assert len(rows) == 1, f"one acceptance has one ledger row, got {rows}"
    return rows[0]


def _detail(box: CancelSandbox) -> str:
    detail = asyncio.run(_debt_probe(box).check())
    assert detail is not None
    return detail


def _scopes(detail: str) -> str:
    """The counted scopes only, without the head or the per-Job drill-down.

    ``cancellation_debt:`` and the counted unit ``cancellation(s)`` both contain a
    parenthesis, so the scope list is the last parenthesised run before ``): ``.
    """
    end = detail.index("): ")
    return detail[detail.rindex("(", 0, end) + 1 : end]


def test_the_read_face_separates_a_retry_from_a_spent_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Job, three instants, three different sentences -- all written by the worker.

    The same acceptance is read while it is claimable now, while a bounded backoff is
    pending, and after the attempt counter reached the storage bound. Before #1038 the
    first two differed but the second and third were the same sentence: the retry count
    and the pending claim time were not selected at all, so "still failing after 64
    tries" and "about to be retried" were indistinguishable on the polled surface.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        # The Job is already `cancelled` (the stop was proven) and its lease is still
        # `reserved`: the release is what keeps failing. The acceptance is inside the
        # bounded read's window, which is the read that names the held lease here.
        _settle_job(box, "cancelled")
        _accept_recently(box, age_us=ACCEPT_AGE_SECONDS * SECOND_US)
        _install_client(monkeypatch, box.job_id)
        reconciler = _retrying_reconciler(box)

        # 1) A pass whose claim instant is older than the schedule it recorded: a worker
        #    that stopped after writing the row leaves exactly this shape, and the row is
        #    claimable now.
        first = asyncio.run(
            reconciler.run_once(now_us=_database_now(box) - 2 * BACKOFF_SECONDS * SECOND_US)
        )
        assert (first.claimed, first.exhausted, first.retried) == (1, 0, 1)
        assert first.blocked, "a refused release must be reported, not settled"
        row = _ledger(box)
        assert (row["attempts"], row["last_outcome"]) == (1, "stop_unconfirmed")
        assert row["settled_at_us"] is None

        due_now = _detail(box)
        assert "due now: 1" in due_now
        assert f"{LEFT_CANCELLING_HOLDING_CAPACITY}: 1" in due_now
        assert "retrying on schedule" not in due_now
        assert RETRY_AT_ATTEMPT_CEILING not in due_now

        # 2) The next claim happens at the instant the ledger named, so a bounded backoff
        #    is pending again: the state the detail used to compute and drop. The retry
        #    facts are in the drill-down, next to the Job and its held lease.
        second = asyncio.run(reconciler.run_once(now_us=int(row["next_check_at_us"])))
        assert (second.claimed, second.retried) == (1, 1)
        row = _ledger(box)
        assert (row["attempts"], row["last_outcome"]) == (2, "stop_unconfirmed")
        assert int(row["next_check_at_us"]) > _database_now(box)

        retrying = _detail(box)
        assert "due now" not in retrying
        # Exact scope list: the four ledger marks are mutually exclusive, the retry
        # state appears in its fixed place, and the ceiling fact is absent.
        assert (
            "(held leases: execution-unknown=1; retrying on schedule: 1; "
            f"{LEFT_CANCELLING_HOLDING_CAPACITY}: 1)" in retrying
        )
        assert re.search(r"\[retrying: attempts=2, next in \d+s\]", retrying)
        assert RETRY_AT_ATTEMPT_CEILING not in retrying

        # 3) Keep claiming exactly when the ledger says to. The claim clamps
        #    ``attempts`` at the storage bound instead of failing, so this is the real
        #    saturation path -- no UPDATE, no seeded row.
        for _ in range(ATTEMPT_CEILING - 2):
            asyncio.run(reconciler.run_once(now_us=int(_ledger(box)["next_check_at_us"])))
        saturated_row = _ledger(box)
        assert (saturated_row["attempts"], saturated_row["last_outcome"]) == (
            ATTEMPT_CEILING,
            "stop_unconfirmed",
        )
        assert saturated_row["settled_at_us"] is None
        # The row is still scheduled: saturating the counter does not stop the claims.
        assert int(saturated_row["next_check_at_us"]) > _database_now(box)

        saturated = _detail(box)

    # The retry is still named -- it is a backoff that keeps running -- and the counter
    # fact is named beside it, after the four marks and before the Job-state label.
    assert "retrying on schedule: 1" in saturated
    assert (
        "(held leases: execution-unknown=1; retrying on schedule: 1; "
        f"{RETRY_AT_ATTEMPT_CEILING}: 1; {LEFT_CANCELLING_HOLDING_CAPACITY}: 1)" in saturated
    )
    assert re.search(rf"\[retrying: attempts={ATTEMPT_CEILING}, next in \d+s\]", saturated)

    # The two instants are distinguishable, and the added entry is exactly the ceiling
    # fact at its fixed position: the normal-backoff sentence carries none of it, and
    # every other scope is unchanged, in the same order.
    assert RETRY_AT_ATTEMPT_CEILING not in retrying
    assert _scopes(saturated) == _scopes(retrying).replace(
        "retrying on schedule: 1", f"retrying on schedule: 1; {RETRY_AT_ATTEMPT_CEILING}: 1"
    )


def test_the_ceiling_fact_is_counted_per_job_not_per_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counter lives on the ledger row, so a second held lease cannot double it.

    A saturated Job with two held leases repeats the same ``attempts`` on both rows --
    one ledger row, two leases -- and the scope must still say one Job, exactly as the
    schedule marks do. Two leases also keep the ceiling fact in the same sentence as a
    non-trivial ``held lease(s)`` count, so the two units cannot be confused.
    """
    with _sandbox(
        (LEASE, QUARANTINED_DOCUMENT_STATE), (LEASE_SECOND, QUARANTINED_DOCUMENT_STATE)
    ) as box:
        _settle_job(box, "cancelled")
        _accept_recently(box, age_us=ACCEPT_AGE_SECONDS * SECOND_US)
        _install_client(monkeypatch, box.job_id)
        reconciler = _retrying_reconciler(box)
        now_us = _database_now(box)
        for _ in range(ATTEMPT_CEILING):
            asyncio.run(reconciler.run_once(now_us=now_us))
            now_us = int(_ledger(box)["next_check_at_us"])
        assert _ledger(box)["attempts"] == ATTEMPT_CEILING

        detail = _detail(box)

    assert "over 2 held lease(s)" in detail
    assert "held leases: execution-unknown=2" in detail
    assert f"{RETRY_AT_ATTEMPT_CEILING}: 1" in detail
    assert "retrying on schedule: 1" in detail
