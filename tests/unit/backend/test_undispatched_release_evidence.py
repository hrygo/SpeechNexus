"""`not_dispatched` is a second success proof, and an unknown kind never is one.

The gateway now closes an execution whose stage issued zero engine requests with
`stop_evidence_kind=not_dispatched`: nothing was running, so nothing needed stopping.
These tests pin the two halves of that change on the consumer side -- the release
authority accepts the new kind, and a reader that does not know it keeps failing closed
instead of reading it as success.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from speechnexus_backend.speech.domain.job_dispatch import (
    SUCCESS_STOP_EVIDENCE_KINDS,
    VERIFIED_STOP_EVIDENCE_KINDS,
)
from speechnexus_backend.speech.domain.lease_release import (
    LeaseStop,
    ReleaseLease,
    SubmitReleaseFacts,
    release,
)
from speechnexus_backend.speech.domain.leases import LeaseRejectedError
from speechnexus_backend.speech.infrastructure.asr_settlement_debt import summarize
from tests.unit.test_production_leases import LEASE, LEDGER

_DIGEST = "sha256:" + "b" * 64


def _facts(kind: str | None, *, state: str = "succeeded") -> SubmitReleaseFacts:
    return SubmitReleaseFacts(
        "submit-t",
        state,
        "complete",
        None if kind is None else LeaseStop("submit-t", kind, _DIGEST, 100),
    )


def _decide(execution: SubmitReleaseFacts, **changes: Any) -> Any:
    return release(
        changes.pop("lease", LEASE),
        changes.pop("accounting", LEDGER),
        ReleaseLease(LEASE.lease_id, LEASE.version),
        execution=execution,
        other_leases_released=changes.pop("other_leases_released", True),
        now_us=changes.pop("now_us", 101),
    )


def test_the_new_kind_is_a_verified_stop_for_both_readers() -> None:
    assert "not_dispatched" in VERIFIED_STOP_EVIDENCE_KINDS
    assert frozenset({"engine_terminal_response", "not_dispatched"}) == SUCCESS_STOP_EVIDENCE_KINDS
    # Constructing the stop is itself the reader's admission step: it refuses any kind
    # the single definition point does not list.
    assert LeaseStop("submit-t", "not_dispatched", _DIGEST, 100).kind == "not_dispatched"


def test_a_succeeded_execution_without_dispatch_is_releasable() -> None:
    decision = _decide(_facts("not_dispatched"))

    assert decision.lease.lease_state == "released"
    assert decision.lease.cleanup_state == "complete"
    assert decision.lease.budget_released_at_us == 101
    assert decision.accounting.held_slots == LEDGER.held_slots - LEASE.reserved_slots


@pytest.mark.parametrize("kind", ["instance_terminated_by_controller", "engine_abort_ack"])
def test_a_succeeded_execution_still_needs_a_success_proof(kind: str) -> None:
    # Every other verified kind describes a stop forced from outside. Accepting one here
    # would report engine-free success for an execution nobody can show finished.
    with pytest.raises(LeaseRejectedError, match="execution_cleanup_pending"):
        _decide(_facts(kind))


def test_a_succeeded_execution_without_any_stop_evidence_stays_held() -> None:
    with pytest.raises(LeaseRejectedError, match="execution_cleanup_pending"):
        _decide(_facts(None))


def test_an_unknown_kind_fails_closed_for_an_old_reader() -> None:
    # A reader built before this change knows only the pre-existing set. It cannot admit
    # the new kind, and it must not treat the row as settled either.
    with pytest.raises(ValueError, match="Unverified lease stop kind"):
        LeaseStop("submit-t", "not_dispatched_v2", _DIGEST, 100)
    with pytest.raises(ValueError, match="Unverified lease stop kind"):
        _facts("not_dispatched_v2")


def test_the_debt_probe_does_not_read_the_new_kind_as_settled() -> None:
    # A held `succeeded` execution is debt whatever closed it: the probe's job is to name
    # capacity that is still held, so it keeps its engine-terminal-only proof rule.
    debt = summarize(
        [
            {
                "operation_id": "operation-undispatched",
                "execution_state": "succeeded",
                "cleanup_state": "complete",
                "evidence_kind": "not_dispatched",
                "execution_stopped": 1,
                "resources_released": 1,
                "overdue_us": 5,
                "gateway_id": "gateway-a",
                "host_node_id": "host-a",
                "settled_at_us": None,
            }
        ],
        ("gateway-a", "host-a"),
    )

    assert (debt.cleanup_pending, debt.unknown, debt.settled_but_held) == (1, 0, 0)
    assert debt.samples == ("cleanup_pending:operation-undispatched:overdue_us=5",)


def test_a_failed_execution_keeps_its_own_rule() -> None:
    # The new kind must not smuggle a stop into the failed/not_required branch.
    facts = replace(
        _facts("not_dispatched"), execution_state="failed", cleanup_state="not_required"
    )
    with pytest.raises(LeaseRejectedError, match="execution_cleanup_pending"):
        _decide(facts)
