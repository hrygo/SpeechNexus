"""#1008: the cancellation debt that outlived every bounded read face.

The permanent shape is three durable facts that are each individually legitimate:

    speech_jobs.state                            cancelled
    admission_leases.lease_state                 reserved
    speech_job_cancellation_reconciliations      last_outcome = 'exhausted',
                                                 settled_at_us IS NULL

and one more that made it invisible: the fence's ``accepted_at_us`` older than the
fourth read's lookback. ``settle()`` returns "settled" for a Job that is already
``cancelled`` without isolating its leases, so the ledger records ``exhausted``
while the capacity is still held. After that:

* the two Job-state reads need ``state = 'cancelling'`` and cannot see it;
* ``_LEFT_CANCELLING_DEBT`` is bounded by acceptance age and stops naming it;
* the reconciliation worker's claim excludes ``exhausted``, so nothing revisits it.

The result was capacity that never returns and a diagnostic that never names it --
the oldest, worst debt was the one the read face was blind to. This module pins the
shape to the production participants (the real settlement judgement, the real
reconciliation ledger, the real ``PublicJobs`` transition) and asserts that the
fifth read names it with no age bound, that it does so exactly once, and that the
bounded read keeps the acceptances it still covers.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_cancel_failure_slot_pg import (
    LEASE,
    LEASE_SECOND,
    PASSWORD,
    QUARANTINED_DOCUMENT_STATE,
    VARIABLE,
    CancelSandbox,
    _add_acceptance,
    _database_now,
    _debt_probe,
    _left_cancelling_rows,
    _sandbox,
    _settle_job,
)

from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    RECONCILIATION_TABLE,
)
from speechnexus_backend.speech.infrastructure.readiness import (
    ABANDONED_HOLDING_CAPACITY,
    LEFT_CANCELLING_HOLDING_CAPACITY,
    CancellationDebtProbe,
)

pytestmark = pytest.mark.integration

HOUR_US = 3_600 * 1_000_000
# The fourth read's documented default window, spelled out here so a change to it
# cannot silently make these cases pass for the wrong reason.
WINDOW_24H_US = 24 * HOUR_US


def _accepted_before(box: CancelSandbox, *, age_us: int) -> int:
    """Move the fence's acceptance ``age_us`` into the past, on the database clock."""
    accepted_at_us = _database_now(box) - age_us
    box.execute(
        "UPDATE speech_job_cancellation_fences SET accepted_at_us = %s WHERE job_id = %s",
        (accepted_at_us, box.job_id),
    )
    return accepted_at_us


def _record_exhausted(box: CancelSandbox) -> None:
    """The permanent shape, seeded as the durable state an earlier build left.

    Before #1033 the worker itself wrote it: ``settle()`` returned
    ``settled=True``/``exhausted`` for a Job that was already ``cancelled`` --
    without releasing its lease, because the release belongs to the release path
    -- and the reconciler stored that verdict. A judgement whose release is still
    refused now stays claimable instead, so the production chain no longer
    produces this row; a deployment upgraded from an earlier build, or one whose
    worker stopped while the row was exhausted, still stores it. The read face
    must keep naming that debt, which is what this fixture preserves.
    """
    _settle_job(box, "cancelled")
    box.execute(
        f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
        "last_outcome, settled_at_us, updated_at_us) VALUES (%s, 1, 0, 'exhausted', NULL, 1)",
        (box.job_id,),
    )


def _probe_with_grace(box: CancelSandbox, *, grace_seconds: float) -> CancellationDebtProbe:
    """The same probe with a realistic grace.

    The shared fixture sets ``grace_seconds=0.001`` so the offline cases can be
    written at fixed instants; a test about the grace premise needs the real one.
    """
    return CancellationDebtProbe(
        make_conninfo(os.environ[VARIABLE], user=box.role, password=PASSWORD),
        schema=box.schema,
        timeout_seconds=3,
        grace_seconds=grace_seconds,
    )


def _left_read(box: CancelSandbox, *, window_us: int = WINDOW_24H_US) -> list[dict[str, object]]:
    """The fourth read at its shipped window, pinned to one database instant."""
    return _left_cancelling_rows(box, now_us=_database_now(box), window_us=window_us, grace_us=0)


@pytest.mark.parametrize("age_us", [25 * HOUR_US, 100 * 24 * HOUR_US])
def test_the_permanent_shape_is_named_by_the_unbounded_read(age_us: int) -> None:
    """#1008: the shape is named again, however old the acceptance is.

    The two ages are the issue's 25 h (one hour past the window) and a 100-day debt:
    a read that still passes only because the row is just outside one particular
    window would fail the second case, which is what makes "no age bound" testable
    rather than a claim.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        _record_exhausted(box)
        _accepted_before(box, age_us=age_us)

        rows = box.rows()
        # The exact three facts the issue names, after the real participants ran.
        assert rows["speech_jobs"][0]["state"] == "cancelled"
        assert rows["admission_leases"][0]["lease_state"] == "reserved"
        assert rows["ledger"] == [
            {
                "job_id": box.job_id,
                "attempts": 1,
                "last_outcome": "exhausted",
                "settled_at_us": None,
            }
        ]
        # Capacity never came back: the instance slot is still held.
        assert rows["engine_instances"] == [
            {"instance_id": "instance-test", "held_slots": 1, "quarantined_slots": 0}
        ]

        # The bounded read no longer names it: this is the escape the issue recorded.
        assert _left_read(box) == []

        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert detail.startswith(
        "cancellation_debt: 1 accepted cancellation(s) still hold capacity over 1 held lease(s)"
    )
    assert (
        f"{box.job_id} -> {LEASE}(lease=reserved, execution=unknown_quarantined, "
        "subclass=execution-unknown)" in detail
    )
    # The ledger fact that proves nothing automatic will claim it again, and the
    # label that proves which read found it.
    assert "never claimable (exhausted): 1" in detail
    assert f"{ABANDONED_HOLDING_CAPACITY}: 1" in detail
    # The bounded label is the fourth read's; it must not claim a Job it cannot see.
    assert LEFT_CANCELLING_HOLDING_CAPACITY not in detail
    # One Job, one lease, one line: the fifth read did not name it twice.
    assert detail.count(box.job_id) == 1


def test_an_acceptance_inside_the_window_stays_with_the_bounded_read() -> None:
    """The two labels hand the Job over; they never both describe it.

    The fifth read overlaps the fourth by construction -- no Job-state gate, no age
    bound -- so a two-hour-old acceptance is returned by both. The label still moves
    with the window: inside it the bounded read owns the Job, outside it the
    unbounded one does. Counting follows the same rule, so the lease is named once
    and the Job count is not inflated.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        _record_exhausted(box)
        _accepted_before(box, age_us=2 * HOUR_US)

        assert [row["job_id"] for row in _left_read(box)] == [box.job_id]
        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert detail.startswith(
        "cancellation_debt: 1 accepted cancellation(s) still hold capacity over 1 held lease(s)"
    )
    assert f"{LEFT_CANCELLING_HOLDING_CAPACITY}: 1" in detail
    assert ABANDONED_HOLDING_CAPACITY not in detail
    assert "never claimable (exhausted): 1" in detail
    assert detail.count(LEASE) == 1


def test_an_acceptance_inside_the_grace_is_not_abandoned() -> None:
    """The fifth read keeps the premise every cancellation read shares: past the grace.

    Without it the read would name every in-flight cancellation as abandoned for the
    900 s the grace exists for -- capacity that is about to be returned is not debt.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        _record_exhausted(box)
        # The acceptance is "now": a 900 s grace has not elapsed. The Job is already
        # ``cancelled``, so only the fifth read could name it -- and it must not.
        _accepted_before(box, age_us=0)

        detail = asyncio.run(_probe_with_grace(box, grace_seconds=900).check())

    assert detail is None


def test_a_cancellation_the_worker_never_claimed_is_named_too() -> None:
    """The shape a ledger-driven read cannot see: accepted, held, and no ledger row.

    Default deployments run with reconciliation disabled, so a refused release may never
    be claimed at all -- no ledger row exists, and once the acceptance is past the fourth
    read's window nothing names it. The fifth read starts from the held lease, so it does.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        box.execute(
            "UPDATE speech_jobs SET state = 'cancelled', status_version = 3 WHERE job_id = %s",
            (box.job_id,),
        )
        _accepted_before(box, age_us=25 * HOUR_US)
        # No ledger row exists: the worker never claimed the acceptance.
        assert box.rows()["ledger"] == []
        assert _left_read(box) == []

        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert detail.startswith(
        "cancellation_debt: 1 accepted cancellation(s) still hold capacity over 1 held lease(s)"
    )
    # The mark that says the deployment never drove this acceptance, and the label that
    # says no bounded read names it any more.
    assert "never claimed: 1" in detail
    assert f"{ABANDONED_HOLDING_CAPACITY}: 1" in detail
    assert LEFT_CANCELLING_HOLDING_CAPACITY not in detail
    assert "unavailable" not in detail


def test_a_held_lease_without_an_accepted_cancellation_is_not_debt() -> None:
    """Capacity held by a Job nobody asked to cancel is a different fact.

    The fifth read joins the fence, so a lease of a live Job -- no accepted cancellation
    -- is not cancellation debt even though it holds capacity.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        box.execute(
            "UPDATE speech_jobs SET state = 'cancelled', status_version = 3 WHERE job_id = %s",
            (box.job_id,),
        )
        _accepted_before(box, age_us=25 * HOUR_US)
        box.execute("DELETE FROM speech_job_cancellation_fences WHERE job_id = %s", (box.job_id,))

        detail = asyncio.run(_debt_probe(box).check())

    assert detail is None


def test_a_cancelling_job_is_not_tagged_abandoned() -> None:
    """The fifth tag means "no read named it", so a live ``cancelling`` Job never has it.

    A ``cancelling`` Job is named by the state-gated read, which has no window at all.
    Both reads therefore return this row; folding them together would tag every
    ordinary cancellation as abandoned and delete the distinction.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        box.execute(
            f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
            "last_outcome, settled_at_us, updated_at_us) VALUES (%s, 8, 0, 'exhausted', NULL, 1)",
            (box.job_id,),
        )
        _accepted_before(box, age_us=25 * HOUR_US)

        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert "never claimable (exhausted): 1" in detail
    assert ABANDONED_HOLDING_CAPACITY not in detail
    assert LEFT_CANCELLING_HOLDING_CAPACITY not in detail


def test_a_settled_ledger_is_not_debt_for_the_unbounded_read() -> None:
    """The fifth read keeps the ledger's settlement rule; it does not widen the debt.

    A settled ledger says the acceptance is closed. Capacity still held in that shape
    belongs to the release path, not to this diagnostic, and reading it here would
    report ordinary successful cancellations as permanent debt.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        _record_exhausted(box)
        _accepted_before(box, age_us=25 * HOUR_US)
        box.execute(
            f"UPDATE {RECONCILIATION_TABLE} SET last_outcome = 'settled', settled_at_us = %s "
            "WHERE job_id = %s",
            (_database_now(box), box.job_id),
        )

        assert box.rows()["ledger"][0]["settled_at_us"] is not None
        assert asyncio.run(_debt_probe(box).check()) is None


def test_the_unbounded_read_requires_capacity_that_is_still_held() -> None:
    """A released or quarantined lease returned its capacity, so it is not this debt.

    Without the capacity rule the read would report every old exhausted ledger row --
    including the ones whose leases were accounted for -- and the label would stop
    pointing at held slots.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        _record_exhausted(box)
        _accepted_before(box, age_us=25 * HOUR_US)

        for accounted in ("released", "quarantined"):
            box.execute(
                "UPDATE admission_leases SET lease_state = %s WHERE operation_id = %s",
                (accounted, box.job_id),
            )
            assert asyncio.run(_debt_probe(box).check()) is None


def test_both_leases_of_one_abandoned_job_are_one_job() -> None:
    """A multi-stage Job holds two slots but is one accepted cancellation."""
    with _sandbox(
        (LEASE, QUARANTINED_DOCUMENT_STATE), (LEASE_SECOND, QUARANTINED_DOCUMENT_STATE)
    ) as box:
        _record_exhausted(box)
        _accepted_before(box, age_us=25 * HOUR_US)

        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert detail.startswith(
        "cancellation_debt: 1 accepted cancellation(s) still hold capacity over 2 held lease(s)"
    )
    assert f"{ABANDONED_HOLDING_CAPACITY}: 1" in detail
    assert "held leases: execution-unknown=2" in detail
    # Each lease is named exactly once: the row that arrived from both reads was
    # counted as one held lease, not two.
    assert detail.count(LEASE) == 1
    assert detail.count(LEASE_SECOND) == 1


def test_the_unbounded_read_does_not_widen_the_job_count() -> None:
    """A permanent Job and a live one stay two Jobs, two leases, two marks.

    The other reads must keep working unchanged beside the new one: this is the
    zero-regression check for the existing scopes in the presence of the fifth.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        _record_exhausted(box)
        _accepted_before(box, age_us=25 * HOUR_US)
        # A second, ordinary accepted cancellation with no lease: `no-held-lease`.
        _add_acceptance(box, ledger=("pending", 1, 0))

        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert detail.startswith(
        "cancellation_debt: 2 accepted cancellation(s) still hold capacity over 1 held lease(s)"
    )
    assert "jobs: no-held-lease=1" in detail
    assert "due now: 1" in detail
    assert "never claimable (exhausted): 1" in detail
    assert f"{ABANDONED_HOLDING_CAPACITY}: 1" in detail


def test_a_role_without_the_ledger_grants_still_degrades_instead_of_failing() -> None:
    """The fifth read needs the same grants, so its refusal is the same degradation.

    The probe may never turn a missing privilege into an outage: the deployment that
    does not provision the read-only role must still get the Job names and one
    ``unavailable (<category>)`` note, not a failed healthcheck.
    """
    with _sandbox(
        (LEASE, QUARANTINED_DOCUMENT_STATE), coordination=False, api_identity=True
    ) as box:
        # Seeded as the schema owner: the identity under test legitimately cannot
        # write the Job row, and this case is about what the probe reads.
        box.execute(
            "UPDATE speech_jobs SET state = 'cancelled', status_version = 3 WHERE job_id = %s",
            (box.job_id,),
        )
        box.execute(
            f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
            "last_outcome, settled_at_us, updated_at_us) VALUES (%s, 8, 0, 'exhausted', NULL, 1)",
            (box.job_id,),
        )
        _accepted_before(box, age_us=25 * HOUR_US)

        detail = asyncio.run(_debt_probe(box).check())

    # The API identity is granted none of the fence/ledger/lease columns, so the
    # sentence is exactly the documented degradation -- no invented Job, a named
    # category -- and never the fifth read's label.
    assert detail == "cancellation_debt: unavailable (permission)"


def test_the_bounded_read_is_unchanged_by_the_new_scope() -> None:
    """#1007 stays intact: the fourth read still names a Job inside its window.

    This is the direct zero-regression assertion for the read the fifth was added
    beside -- it must still return the row, with its own label, at the documented
    window.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        _settle_job(box, "cancelled")
        _accepted_before(box, age_us=2 * HOUR_US)

        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert f"{LEFT_CANCELLING_HOLDING_CAPACITY}: 1" in detail
    assert "never claimed: 1" in detail
    assert ABANDONED_HOLDING_CAPACITY not in detail


def test_the_optional_reader_identity_sees_the_fifth_scope_without_new_grants() -> None:
    """The fifth read needs no column migration 0061 did not already grant.

    ``cancellation_debt_reader_grants`` is the whole authorization for this detail and
    this slice adds an index, not a grant. Pointing the probe at exactly that role
    must therefore name the permanent shape -- and must never require the API identity
    to be widened.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE), coordination=False, debt_reader=True) as box:
        # Seeded as the schema owner: the read-only identity holds no write grant.
        box.execute(
            "UPDATE speech_jobs SET state = 'cancelled', status_version = 3 WHERE job_id = %s",
            (box.job_id,),
        )
        box.execute(
            f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
            "last_outcome, settled_at_us, updated_at_us) VALUES (%s, 8, 0, 'exhausted', NULL, 1)",
            (box.job_id,),
        )
        _accepted_before(box, age_us=25 * HOUR_US)

        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert "unavailable" not in detail
    assert f"{box.job_id} -> {LEASE}(lease=reserved, execution=unknown_quarantined, " in detail
    assert "never claimable (exhausted): 1" in detail
    assert f"{ABANDONED_HOLDING_CAPACITY}: 1" in detail


def test_the_unbounded_read_is_read_only() -> None:
    """The probe still only reads: no ledger, lease or capacity row may move."""
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        _record_exhausted(box)
        _accepted_before(box, age_us=25 * HOUR_US)
        before = box.rows()

        assert asyncio.run(_debt_probe(box).check()) is not None
        after = box.rows()

    assert after == before
