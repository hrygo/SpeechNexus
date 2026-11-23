"""The cancellation fence against the canonical claim and the terminal guard, on real PG.

#1176/#1191 moved the dispatch claim onto the canonical bucket/lease columns; this
slice moves the fence's ordering lock onto the same canonical Job row instead of a
PostgreSQL advisory key. The three product facts this file pins are the ones the
switch can break, and none of them is observable against a fake or a single
sequential call:

1. a Job whose cancellation fence is committed is never claimed again;
2. cancelling a claimed Job makes the worker's next ``_finish`` refuse;
3. two concurrent cancellations of one Job leave exactly one fence row and both
   callers converge on the first acceptance facts.

The claim side is the real ``PostgresDispatchQueue`` and the real committed schema
(see ``test_dispatch_canonical_claim_pg`` for the fixture's role split); the fence
side is the production ``PostgresPublicJobs.cancel`` transaction, not a bare
``INSERT``. The red injection that turns each case red is recorded with its result
in ``docs/plans/2026-11-21-portable-sql-delivery.md`` §8.78.

The concurrent case pauses the receipt insert with a test-only trigger so the two
acceptances really overlap before the first one commits; without that pause the
second call can finish after the first commit and the test would not exercise the
serialization it claims to.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import Any

import pytest
from tests.integration.test_dispatch_canonical_claim_pg import (
    ClaimSandbox,
    _claims,
    _submit_admitted,
)
from tests.integration.test_dispatch_canonical_claim_pg import (
    sandbox as sandbox,
)

from speechnexus_backend.speech.domain.job_dispatch import DispatchClaimLostError

pytestmark = pytest.mark.integration


def _fence_rows(sandbox: ClaimSandbox, job_id: str) -> list[dict[str, Any]]:
    return sandbox.query(
        "SELECT job_id, accepted_at_us, request_id FROM speech_job_cancellation_fences "
        "WHERE job_id = %s",
        (job_id,),
    )


def test_an_accepted_cancellation_is_never_claimed(sandbox: ClaimSandbox) -> None:
    """The canonical Job row is closed in the acceptance commit, so no CAS can win.

    The injection is deleting the ``UPDATE coordination_jobs SET job_state =
    'cancelled'`` in ``PostgresPublicJobs.cancel``: the fence still commits, the
    canonical row stays ``pending`` and the claim below returns the cancelled Job.
    """

    job_id = _submit_admitted(sandbox, 0)
    accepted = sandbox.jobs.cancel(
        job_id, request_id="fence-claim-1", idempotency_key=None, workspace_id=None
    )

    assert accepted.receipt is not None and accepted.receipt.accepted
    assert accepted.accepted_now
    assert [row["job_id"] for row in _fence_rows(sandbox, job_id)] == [job_id]

    claimed = _claims(sandbox.queue(), "fence-claim-worker", 30_000, 8)

    assert claimed == []
    assert sandbox.canonical(job_id)["job_state"] == "cancelled"
    assert sandbox.canonical(job_id)["claim_owner_id"] is None


def test_cancelling_a_claimed_job_makes_the_next_finish_refuse(sandbox: ClaimSandbox) -> None:
    """Both terminal paths see the acceptance; neither lets the worker close the Job.

    The injection relaxes the three status predicates of ``_finish``: the canonical
    ``job_state = 'claimed'`` guard, the public ``status_version``/``state`` CAS, and
    the canonical close's own ``job_state = 'claimed'`` condition. The public status
    trigger refuses ``cancelling -> succeeded`` anyway, so the legal
    ``cancelling -> failed`` shape is tried first: relaxing the predicates is what
    makes that one commit.
    """

    job_id = _submit_admitted(sandbox, 1)
    claimed = _claims(sandbox.queue(), "fence-finish-worker", 30_000, 8)
    claim = next(c for c in claimed if c.job.job_id == job_id)
    store = sandbox.state_store()
    running = asyncio.run(store.mark_running(claim.job))
    claim = replace(claim, job=running)
    assert running.state == "running"

    accepted = sandbox.jobs.cancel(
        job_id, request_id="fence-finish-1", idempotency_key=None, workspace_id=None
    )

    assert accepted.receipt is not None and accepted.receipt.accepted
    assert sandbox.public(job_id)["state"] == "cancelling"
    assert sandbox.canonical(job_id)["job_state"] == "cancelled"
    # ``cancelling -> failed`` is a legal transition shape while
    # ``cancelling -> succeeded`` is not; try the legal one first so a success
    # cannot be blamed on the public status guard alone.
    with pytest.raises(DispatchClaimLostError):
        asyncio.run(store.finish(claim, "failed", result_json=None, error_code="worker_failed"))
    with pytest.raises(DispatchClaimLostError):
        asyncio.run(store.finish(claim, "succeeded", result_json="{}", error_code=None))
    # The refused close rolled back: no terminal state, no terminal event.
    assert sandbox.public(job_id)["state"] == "cancelling"
    assert sandbox.query(
        "SELECT count(*) AS n FROM speech_job_events "
        "WHERE job_id = %s AND state IN ('succeeded', 'failed')",
        (job_id,),
    ) == [{"n": 0}]


def test_two_concurrent_cancellations_leave_one_fence_row(sandbox: ClaimSandbox) -> None:
    """Serialized acceptance: one row, one acceptance time, one accepted caller.

    The pause trigger makes the first transaction still hold the Job row while the
    second starts, so the second really waits on the acceptance lock instead of
    arriving after the commit. The injection is deleting the
    ``receipt_row is not None`` replay short-circuit in ``PostgresPublicJobs.cancel``:
    the second acceptance then tries to insert a second receipt, the receipt primary
    key aborts it, and the caller sees ``unknown`` instead of the first facts.
    """

    job_id = _submit_admitted(sandbox, 2)
    schema = sandbox.schema
    sandbox.admin.execute(
        f'CREATE FUNCTION "{schema}".fence_pause() RETURNS trigger LANGUAGE plpgsql '
        "AS $$ BEGIN PERFORM pg_sleep(0.3); RETURN NEW; END $$"
    )
    sandbox.admin.execute(
        f'CREATE TRIGGER fence_pause BEFORE INSERT ON "{schema}".speech_job_cancellations '
        f'FOR EACH ROW EXECUTE FUNCTION "{schema}".fence_pause()'
    )

    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}

    def cancel(request_id: str) -> None:
        barrier.wait()
        try:
            outcomes[request_id] = sandbox.jobs.cancel(
                job_id, request_id=request_id, idempotency_key=None, workspace_id=None
            )
        except BaseException as error:
            outcomes[request_id] = error

    threads = [threading.Thread(target=cancel, args=(f"fence-race-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert all(not thread.is_alive() for thread in threads)

    errors = [value for value in outcomes.values() if isinstance(value, BaseException)]
    assert errors == []
    views = list(outcomes.values())
    assert len({view.accepted_now for view in views}) == 2
    assert sum(1 for view in views if view.accepted_now) == 1
    receipts = [view.receipt for view in views if view.receipt is not None]
    assert len(receipts) == len(views)
    first_facts = {(receipt.request_id, receipt.requested_at_us) for receipt in receipts}
    assert len(first_facts) == 1
    request_id, accepted_at_us = first_facts.pop()

    rows = _fence_rows(sandbox, job_id)
    assert len(rows) == 1
    assert rows[0]["request_id"] == request_id
    assert rows[0]["accepted_at_us"] == accepted_at_us
    assert sandbox.query(
        "SELECT count(*) AS n FROM speech_job_cancellations WHERE job_id = %s", (job_id,)
    ) == [{"n": 1}]
