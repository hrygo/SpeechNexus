"""Release evidence, quota finality and original-snapshot recovery decisions."""

from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.lease_release import (
    LeaseStop,
    ReleaseLease,
    SubmitReleaseFacts,
    release,
    release_already_recorded,
    require_complete_stages,
)
from speechnexus_backend.speech.domain.leases import LeaseRejectedError, LeaseResult
from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.interfaces.lease_transactions import LeaseTransactionBoundary
from tests.unit.test_production_leases import LEASE, LEDGER, VECTOR, FaultPool, Port, decide

REQUEST = ReleaseLease(LEASE.lease_id, 1)
STOP = LeaseStop("submit-test", "engine_terminal_response", "sha256:" + "e" * 64, 100)
SUCCESS = SubmitReleaseFacts("submit-test", "succeeded", "complete", STOP)


def result(**changes: Any) -> Any:
    return release(
        changes.pop("lease", LEASE),
        changes.pop("accounting", LEDGER),
        changes.pop("request", REQUEST),
        execution=changes.pop("execution", SUCCESS),
        other_leases_released=changes.pop("last", True),
        now_us=changes.pop("now_us", 101),
        **changes,
    )


def test_last_release_returns_only_frozen_increments_and_one_attempt_quota() -> None:
    settled = result()
    assert settled.lease.lease_state == "released" and settled.lease.cleanup_state == "complete"
    assert settled.lease.budget_released_at_us == 101 and settled.lease.version == 2
    assert (
        settled.accounting.held == VECTOR and settled.accounting.quarantined == LEDGER.quarantined
    )
    assert settled.accounting.held_slots == 1
    assert settled.accounting.held_attempts == settled.accounting.quarantined_attempts == 0
    assert settled.accounting.quota_state == "released" and settled.accounting.quota_version == 2


@pytest.mark.parametrize(
    "kind,state",
    [
        ("engine_terminal_response", "succeeded"),
        ("engine_terminal_response", "failed"),
        ("engine_abort_ack", "aborted"),
        ("instance_terminated_by_controller", "aborted"),
        ("operator_verified_instance_terminated", "aborted"),
    ],
)
def test_terminal_cleanup_requires_one_of_the_verified_stop_kinds(kind: str, state: str) -> None:
    execution = replace(SUCCESS, execution_state=state, stop=replace(STOP, kind=kind))
    assert result(execution=execution).lease.lease_state == "released"


@pytest.mark.parametrize(
    "execution", [None, SubmitReleaseFacts("submit-test", "failed", "not_required")]
)
def test_never_submitted_or_native_no_dispatch_failure_can_release_held_lease(
    execution: Any,
) -> None:
    settled = result(execution=execution, last=False)
    assert settled.accounting.held == VECTOR and settled.accounting.held_attempts == 1
    assert settled.accounting.quota_version == LEDGER.quota_version


def test_unknown_quarantine_requires_controller_stop_and_does_not_reactivate_host() -> None:
    isolated = decide()
    ledger = replace(isolated.accounting, node_state="quarantined")
    command = replace(REQUEST, expected_version=isolated.lease.version)
    for execution in (
        None,
        SubmitReleaseFacts("submit-test", "failed", "not_required"),
        SubmitReleaseFacts("submit-test", "unknown_quarantined", "quarantined"),
        SUCCESS,
        SubmitReleaseFacts(
            "submit-test", "aborted", "complete", replace(STOP, kind="engine_abort_ack")
        ),
    ):
        with pytest.raises(LeaseRejectedError, match="execution_cleanup_pending"):
            result(lease=isolated.lease, accounting=ledger, request=command, execution=execution)
    stopped = SubmitReleaseFacts(
        "submit-test",
        "aborted",
        "complete",
        replace(STOP, kind="instance_terminated_by_controller"),
    )
    settled = result(lease=isolated.lease, accounting=ledger, request=command, execution=stopped)
    assert settled.accounting.quarantined.values == (0, 0, 0, 0, 0, 0)
    assert settled.accounting.held == isolated.accounting.held
    assert (
        settled.accounting.quarantined_slots == 0 and settled.accounting.node_state == "quarantined"
    )
    assert settled.lease.observed_at_us == isolated.lease.observed_at_us
    assert settled.lease.reason_code == isolated.lease.reason_code


def test_old_version_release_replays_original_fact_without_reinterpreting_evidence() -> None:
    settled = result()
    replay = result(
        lease=settled.lease,
        accounting=settled.accounting,
        execution=None,
        request=replace(REQUEST, expected_version=MAX_VERSION),
        now_us=200,
    )
    assert replay.replayed and replay.lease is settled.lease
    assert replay.accounting is settled.accounting and replay.lease.budget_released_at_us == 101
    for fact in (
        replace(settled.lease, budget_released_at_us=None),
        replace(settled.lease, budget_released_at_us=201),
        replace(settled.lease, cleanup_state="pending"),
    ):
        with pytest.raises(LeaseRejectedError, match="reconciliation_required"):
            release_already_recorded(fact, REQUEST, 200)


@pytest.mark.parametrize(
    "execution",
    [
        SubmitReleaseFacts("submit-test", "submitting", "pending"),
        SubmitReleaseFacts("submit-test", "executing", "pending"),
        SubmitReleaseFacts("submit-test", "stopping", "pending"),
        SubmitReleaseFacts("submit-test", "unknown_quarantined", "quarantined"),
        SubmitReleaseFacts("submit-test", "succeeded", "complete"),
        SubmitReleaseFacts("submit-test", "failed", "complete"),
        SubmitReleaseFacts("submit-test", "failed", "not_required", STOP),
        replace(SUCCESS, stop=replace(STOP, observed_at_us=102)),
        replace(SUCCESS, stop=replace(STOP, kind="engine_abort_ack")),
    ],
)
def test_cancel_deadline_disconnect_and_unverified_cleanup_never_release(
    execution: SubmitReleaseFacts,
) -> None:
    with pytest.raises(LeaseRejectedError, match="execution_cleanup_pending"):
        result(execution=execution)


@pytest.mark.parametrize(
    "field", ["host_version", "instance_version", "attempt_version", "quota_version"]
)
def test_counter_exhaustion_cannot_partially_settle(field: str) -> None:
    values: dict[str, Any] = {field: MAX_VERSION}
    with pytest.raises(LeaseRejectedError, match="reconciliation_required"):
        result(accounting=replace(LEDGER, **values))


def test_nonfinal_release_keeps_quota_and_attempt_versions_even_when_exhausted() -> None:
    settled = result(
        accounting=replace(LEDGER, attempt_version=MAX_VERSION, quota_version=MAX_VERSION),
        last=False,
    )
    assert settled.accounting.attempt_version == settled.accounting.quota_version == MAX_VERSION
    assert settled.accounting.held_attempts == 1


def test_version_conflict_and_invalid_ledgers_never_publish_a_release() -> None:
    for changes, code in (
        ({"request": replace(REQUEST, expected_version=2)}, "version_conflict"),
        ({"request": replace(REQUEST, lease_id="lease-other")}, "reconciliation_required"),
        ({"lease": replace(LEASE, budget_released_at_us=1)}, "reconciliation_required"),
        (
            {
                "lease": replace(LEASE, version=MAX_VERSION),
                "request": replace(REQUEST, expected_version=MAX_VERSION),
            },
            "reconciliation_required",
        ),
        ({"accounting": replace(LEDGER, held=VECTOR.subtract(VECTOR))}, "reconciliation_required"),
        ({"accounting": replace(LEDGER, held_slots=0)}, "reconciliation_required"),
        ({"accounting": replace(LEDGER, held_attempts=0)}, "reconciliation_required"),
        ({"accounting": replace(LEDGER, quota_state="released")}, "reconciliation_required"),
    ):
        with pytest.raises(LeaseRejectedError, match=code):
            result(**changes)
    with pytest.raises(ValueError):
        result(last=1)


@pytest.mark.parametrize("version", [True, 0, -1, "1", 1.5, 2**63])
def test_release_request_has_strict_bounded_revision(version: Any) -> None:
    with pytest.raises(ValueError):
        ReleaseLease("lease-test", version)


@pytest.mark.parametrize(
    "required,actual",
    [
        ((), ()),
        (("stage-main",), ()),
        (("stage-main",), ("stage-other",)),
        (("stage-main", "stage-main"), ("stage-main",)),
        (("stage-main",), ("stage-main", "stage-main")),
        (("BAD",), ("BAD",)),
    ],
)
def test_last_quota_cannot_be_derived_from_incomplete_or_duplicate_stages(
    required: tuple[str, ...], actual: tuple[str, ...]
) -> None:
    with pytest.raises(LeaseRejectedError, match="reconciliation_required"):
        require_complete_stages(required, actual)


def test_stage_set_order_is_irrelevant_and_forged_stop_identity_is_rejected() -> None:
    require_complete_stages(("stage-main", "stage-other"), ("stage-other", "stage-main"))
    with pytest.raises(ValueError):
        replace(SUCCESS, stop=replace(STOP, submit_intent_id="submit-other"))
    changes: tuple[dict[str, Any], ...] = (
        {"kind": "unconfirmed_timeout"},
        {"kind": "none"},
        {"evidence_digest": "bad"},
    )
    for change in changes:
        with pytest.raises(ValueError):
            replace(STOP, **change)


class ReleasePort(Port):
    def release_lease(self, request: ReleaseLease) -> LeaseResult:
        return LeaseResult("committed", request.lease_id, result().lease)


def test_original_release_contract_uses_decimal_revision_and_complete_snapshot() -> None:
    boundary = LeaseTransactionBoundary(LeaseService(ReleasePort()))
    wire = boundary.release_lease({"lease_id": REQUEST.lease_id, "expected_version": "1"})
    assert wire["value"] == {
        "kind": "lease",
        "lease_id": REQUEST.lease_id,
        "lease_state": "released",
        "cleanup_state": "complete",
        "version": "2",
    }
    for version in (1, True, "0", "01"):
        with pytest.raises(ContractValidationError):
            boundary.release_lease({"lease_id": REQUEST.lease_id, "expected_version": version})


def test_release_transport_ambiguity_is_an_original_query_and_never_write_retry() -> None:
    pool = FaultPool(psycopg.OperationalError("synthetic acknowledgement loss"))
    response = PostgresLeaseTransactions(cast(SpeechPool, pool)).release_lease(REQUEST)
    assert response.outcome == "unknown" and response.recovery == "query_original"
    assert pool.calls == 1
