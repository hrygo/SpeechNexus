"""#1033: the exhausted branch must not report a cancellation whose capacity is held.

The reference window for an accepted, submitted cancellation ends with

    speech_jobs.state                            cancelled
    admission_leases.lease_state                 reserved
    admission_leases.budget_released_at_us       NULL
    engine_instances.held_slots                  1
    speech_job_cancellation_reconciliations      last_outcome = 'exhausted',
                                                 settled_at_us IS NULL

The Job is `cancelled` because the stop was proven; what failed is the capacity
release. Every retry that the confirmation port performs only runs after a
gateway fact it can settle, so a submitted execution stuck in
``unknown_quarantined`` never reaches the release again. Once ``attempts`` passed
``max_attempts`` the judgment took the Job state alone as proof and recorded
``exhausted``; ``claim_due`` excludes that value, so no later pass could return
the slot. The ledger said "accepted and judged" while the instance kept the slot
reserved.

This module drives the production participants against real PostgreSQL: the real
reconciliation ledger and claim predicate, the real gateway document
translation, the real ``PublicJobs`` transition, and the real ``LeaseService``
release. The gateway HTTP client is a fake, and the release failures are real
domain refusals caused by the seeded execution facts, not injected exceptions.
Nothing here is engine or GPU evidence.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.integration.test_cancel_failure_slot_pg import (
    ACCEPTED_AT_US,
    DIGEST,
    LEASE,
    QUARANTINED_DOCUMENT_STATE,
    CancelSandbox,
    _install_client,
    _reconciler,
    _sandbox,
    _settle_job,
)

from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    JobCancellationReconciler,
    ReconciliationBatch,
)
from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    RECONCILIATION_TABLE,
)

pytestmark = pytest.mark.integration
# The default deployment's attempt budget is larger, but the branch is the same;
# three keeps the case short without changing which pass exhausts it.
MAX_ATTEMPTS = 3
# Every pass lands in the fake now timeline, so a 10 s step always clears the
# current backoff (bounded at 4 s below) without racing the DB clock.
STEP_US = 10_000_000
BASE_BACKOFF_US = 1_000_000
MAX_BACKOFF_US = 4_000_000
# The claim's CHECK-bound saturation point (``LEAST(attempts + 1, 64)``).
ATTEMPT_CEILING = 64


def _ledger(box: CancelSandbox) -> dict[str, Any] | None:
    rows = box.query(
        f"SELECT attempts, last_outcome, settled_at_us, next_check_at_us "
        f"FROM {RECONCILIATION_TABLE} WHERE job_id = %s",
        (box.job_id,),
    )
    assert len(rows) <= 1
    return rows[0] if rows else None


def _lease(box: CancelSandbox) -> dict[str, Any]:
    rows = box.query(
        "SELECT lease_state, cleanup_state, budget_released_at_us "
        "FROM admission_leases WHERE lease_id = %s",
        (LEASE,),
    )
    assert len(rows) == 1
    return rows[0]


def _capacity(box: CancelSandbox) -> tuple[int, int, int]:
    instance = box.query(
        "SELECT held_slots, quarantined_slots FROM engine_instances WHERE instance_id = %s",
        ("instance-test",),
    )
    quota = box.query(
        "SELECT held_attempts, quarantined_attempts FROM workspace_quotas WHERE workspace_id = %s",
        ("workspace-test",),
    )
    assert len(instance) == 1 and len(quota) == 1
    assert instance[0]["quarantined_slots"] == 0 and quota[0]["quarantined_attempts"] == 0
    return (
        int(instance[0]["held_slots"]),
        int(quota[0]["held_attempts"]),
        int(instance[0]["quarantined_slots"]),
    )


def _assert_ledger_lease_and_capacity_agree(box: CancelSandbox) -> None:
    """The invariant #1033 is about: no completed signal over held capacity.

    Three durable facts must tell one story. A ledger row may only look closed
    once the lease returned -- the shape that produced the defect was ``exhausted``
    (the store's "never claim again" marker) next to ``reserved`` and
    ``held_slots = 1``.
    """
    ledger, lease = _ledger(box), _lease(box)
    held_slots, held_attempts, _ = _capacity(box)
    holds_capacity = lease["lease_state"] == "reserved" and lease["budget_released_at_us"] is None
    says_closed = ledger is not None and (
        ledger["settled_at_us"] is not None or ledger["last_outcome"] == "exhausted"
    )
    assert not (says_closed and holds_capacity), (
        "the ledger reports a closed acceptance while the lease and the instance "
        f"still hold capacity: ledger={ledger} lease={lease}"
    )
    # The accounting projections must agree with the lease itself, in both directions.
    assert (held_slots, held_attempts) == ((1, 1) if holds_capacity else (0, 0)), (
        f"held slots/attempts disagree with {lease}"
    )


def _run(reconciler: JobCancellationReconciler, now_us: int) -> ReconciliationBatch:
    return asyncio.run(reconciler.run_once(now_us=now_us))


def _settle_verified_stop(box: CancelSandbox) -> None:
    """The gateway's cleanup evidence for the seeded execution arrives late.

    This is the only thing that can authorize the release for this Job: the real
    ``LeaseService`` reads the submit intent's execution/cleanup facts and the
    immutable stop evidence, then returns the admission lease.
    """
    intent_id = submit_intent_identity(box.job_id)
    box.execute(
        "UPDATE submit_intents SET execution_state = 'aborted', cleanup_state = 'complete', "
        "version = version + 1 WHERE submit_intent_id = %s",
        (intent_id,),
    )
    box.execute(
        "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, evidence_kind, "
        "evidence_digest, observed_at_us, execution_stopped, resources_released) "
        "VALUES ('evidence-1033', %s, 'engine_abort_ack', %s, "
        "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT), 1, 1)",
        (intent_id, DIGEST),
    )


def _exhaust_the_budget(
    box: CancelSandbox, monkeypatch: pytest.MonkeyPatch
) -> tuple[JobCancellationReconciler, list[ReconciliationBatch], list[int], int]:
    """Claim the acceptance until ``attempts`` passes ``max_attempts``."""
    _settle_job(box, "cancelled")
    _install_client(monkeypatch, box.job_id)
    reconciler = _reconciler(box, confirm_timeout_seconds=300, max_attempts=MAX_ATTEMPTS)
    batches: list[ReconciliationBatch] = []
    scheduled: list[int] = []
    now_us = ACCEPTED_AT_US
    for _ in range(MAX_ATTEMPTS + 1):
        batches.append(_run(reconciler, now_us))
        row = _ledger(box)
        assert row is not None
        scheduled.append(int(row["next_check_at_us"]))
        now_us += STEP_US
    return reconciler, batches, scheduled, now_us


def test_the_exhausted_branch_stays_retryable_and_returns_the_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1033: the exhausted pass must keep the release being asked.

    The first ``max_attempts`` passes are ordinary retries: the confirmation port
    queries the gateway, gets ``unknown_quarantined`` and records ``pending``.
    The pass past the budget is the one that used to write ``exhausted`` from the
    Job state alone. Here it must instead count as blocked, leave the row
    claimable with the bounded backoff, and agree with the lease and the
    accounting projections. When the stop evidence arrives the very next due pass
    must return the capacity for real.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        reconciler, batches, _scheduled, now_us = _exhaust_the_budget(box, monkeypatch)
        exhausted_pass = batches[-1]

        assert [(b.claimed, b.settled, b.exhausted) for b in batches[:-1]] == [
            (1, 0, 0)
        ] * MAX_ATTEMPTS
        # The exhausted pass is where the old branch wrote "judged" over a held
        # lease; the three durable facts are asserted together first.
        _assert_ledger_lease_and_capacity_agree(box)
        # The exhausted branch is a blocked judgment, not a settlement.
        assert (
            exhausted_pass.claimed,
            exhausted_pass.settled,
            exhausted_pass.exhausted,
            exhausted_pass.retried,
            exhausted_pass.unavailable,
        ) == (1, 0, 0, 1, 0)
        assert [blocked.job_id for blocked in exhausted_pass.blocked] == [box.job_id]
        assert "refused release" in exhausted_pass.blocked[0].detail

        ledger, lease = _ledger(box), _lease(box)
        assert ledger is not None
        # The row keeps advancing under the reconciler's bounded backoff; a
        # terminal ``exhausted`` here would make ``claim_due`` skip it forever.
        assert (ledger["attempts"], ledger["last_outcome"]) == (
            MAX_ATTEMPTS + 1,
            "stop_unconfirmed",
        )
        assert ledger["settled_at_us"] is None
        assert ledger["next_check_at_us"] == now_us - STEP_US + MAX_BACKOFF_US
        # Capacity is still held: the release was refused by the domain, not lost.
        assert lease == {
            "lease_state": "reserved",
            "cleanup_state": "pending",
            "budget_released_at_us": None,
        }
        assert _capacity(box) == (1, 1, 0)
        _assert_ledger_lease_and_capacity_agree(box)

        # A pass before the backoff elapses must not claim the row: retrying is
        # cheap, but it is not a busy loop.
        assert _run(reconciler, now_us - STEP_US + MAX_BACKOFF_US - 1).claimed == 0

        _settle_verified_stop(box)
        due_us = now_us - STEP_US + MAX_BACKOFF_US
        recovered = _run(reconciler, due_us)

        assert (recovered.claimed, recovered.exhausted) == (1, 1)
        released = _lease(box)
        assert released["lease_state"] == "released"
        assert released["cleanup_state"] == "complete"
        assert released["budget_released_at_us"] is not None
        assert _capacity(box) == (0, 0, 0)
        assert _ledger(box) is not None
        _assert_ledger_lease_and_capacity_agree(box)
        # The acceptance is closed for the claim: its capacity answer is now real.
        assert _run(reconciler, due_us + 3_600_000_000).claimed == 0


def test_a_permanently_refused_release_stays_bounded_and_claimable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retrying branch must not amplify: bounded backoff, saturating attempts.

    A release that never becomes possible (the gateway never proves cleanup) is
    operational debt, not progress. The ledger therefore keeps naming it on the
    existing schedule -- exponential backoff capped at ``max_backoff``, attempt
    count clamped at the storage CHECK bound -- so each due pass performs exactly
    one release attempt and the operator surfaces keep naming the held lease.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        reconciler, _batches, scheduled, now_us = _exhaust_the_budget(box, monkeypatch)
        gaps = [
            scheduled[index] - (ACCEPTED_AT_US + index * STEP_US) for index in range(len(scheduled))
        ]
        # 1 s, 2 s, then the 4 s ceiling -- never above it.
        assert gaps == [1_000_000, 2_000_000, 4_000_000, 4_000_000]
        assert max(gaps) == MAX_BACKOFF_US

        # The next due pass claims again and moves the schedule by the ceiling,
        # not by the attempt count: a permanent refusal cannot grow the wait
        # without bound.
        due_us = now_us - STEP_US + MAX_BACKOFF_US
        after = _run(reconciler, due_us)
        ledger = _ledger(box)
        assert after.claimed == 1 and after.exhausted == 0
        assert ledger is not None
        assert ledger["attempts"] == MAX_ATTEMPTS + 2
        assert ledger["next_check_at_us"] == due_us + MAX_BACKOFF_US

        # At the storage CHECK bound the claim clamps instead of failing: the row
        # must stay claimable, because that is the only automatic way back.
        box.execute(
            f"UPDATE {RECONCILIATION_TABLE} SET attempts = %s, next_check_at_us = 0 "
            "WHERE job_id = %s",
            (ATTEMPT_CEILING, box.job_id),
        )
        clamped = _run(reconciler, due_us + MAX_BACKOFF_US)
        assert (clamped.claimed, clamped.exhausted) == (1, 0)
        saturated = _ledger(box)
        assert saturated is not None and saturated["attempts"] == ATTEMPT_CEILING
        assert _lease(box)["lease_state"] == "reserved"
        assert _capacity(box) == (1, 1, 0)
        _assert_ledger_lease_and_capacity_agree(box)

        # And it is still claimable after that many attempts, at the ceiling gap.
        assert _run(reconciler, due_us + 2 * MAX_BACKOFF_US).claimed == 1
