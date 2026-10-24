"""Quarantine accounting, original snapshots and bounded failure recovery."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.leases import (
    LeaseAccounting,
    LeaseRecord,
    LeaseRejectedError,
    LeaseResult,
    QuarantineLease,
    quarantine,
    rejected,
    unknown,
)
from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.infrastructure.leases_postgres import (
    PostgresLeaseTransactions,
    _BudgetExpiredError,
    _DeadlineCursor,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.interfaces.lease_transactions import LeaseTransactionBoundary

ZERO = PhysicalResources.zero()
VECTOR = PhysicalResources(3, 0, 0, 4, 5, 6)
LEASE = LeaseRecord("lease-test", "executing", "pending", 1, VECTOR, 1)
REQUEST = QuarantineLease("lease-test", "engine_transport_unknown", 100)
LEDGER = LeaseAccounting(
    "unified_memory",
    VECTOR.scaled(10),
    VECTOR.scaled(2),
    ZERO,
    "active",
    250000,
    2,
    0,
    3,
    1,
    0,
    2,
    "held",
    1,
    1,
    1,
    1,
)


def decide(**changes: Any) -> Any:
    return quarantine(
        changes.pop("lease", LEASE),
        changes.pop("accounting", LEDGER),
        changes.pop("request", REQUEST),
        execution=changes.pop("execution", ("unknown_quarantined", "quarantined")),
        now_us=changes.pop("now_us", 101),
        **changes,
    )


def test_isolation_preserves_every_physical_dimension_slots_and_attempt_total() -> None:
    decision = decide()
    assert not decision.replayed and decision.lease.lease_state == "quarantined"
    ledger = decision.accounting
    assert ledger.held.add(ledger.quarantined) == LEDGER.held.add(LEDGER.quarantined)
    assert ledger.held_slots + ledger.quarantined_slots == 2
    assert ledger.held_attempts + ledger.quarantined_attempts == 1
    assert ledger.host_version == ledger.instance_version == ledger.attempt_version == 2
    assert decision.lease.budget_released_at_us is None
    assert decision.lease.observed_at_us == 100


def test_replay_retains_first_observation_after_gateway_stop_and_later_timestamp() -> None:
    first = decide()
    second = decide(
        lease=first.lease,
        accounting=first.accounting,
        request=replace(REQUEST, observed_at_us=102),
        now_us=103,
        execution=("aborted", "complete"),
    )
    assert second.replayed and second.lease is first.lease
    assert second.accounting is first.accounting
    with pytest.raises(LeaseRejectedError, match="idempotency_conflict"):
        decide(
            lease=first.lease,
            accounting=first.accounting,
            request=replace(REQUEST, reason_code="cancellation_unconfirmed"),
        )


def test_second_stage_does_not_move_attempt_quota_twice_and_threshold_disables_host() -> None:
    first = decide()
    second = decide(accounting=first.accounting)
    assert second.accounting.held_attempts == 0 and second.accounting.quarantined_attempts == 1
    assert second.accounting.quota_version == first.accounting.quota_version
    assert second.accounting.node_state == "active"
    assert (
        decide(accounting=replace(LEDGER, threshold_ppm=1)).accounting.node_state == "quarantined"
    )
    assert (
        decide(
            accounting=replace(LEDGER, node_state="offline", threshold_ppm=1)
        ).accounting.node_state
        == "offline"
    )


@pytest.mark.parametrize(
    "execution",
    [
        None,
        ("submitting", "pending"),
        ("running", "pending"),
        ("succeeded", "complete"),
        ("failed", "complete"),
        ("aborted", "complete"),
        ("unknown_quarantined", "pending"),
    ],
)
def test_deadline_cancel_or_terminal_without_unknown_fact_cannot_authorize_isolation(
    execution: Any,
) -> None:
    with pytest.raises(LeaseRejectedError, match="execution_cleanup_pending"):
        decide(execution=execution)


@pytest.mark.parametrize(
    "field", ["host_version", "instance_version", "attempt_version", "quota_version"]
)
def test_exhausted_versions_roll_back_instead_of_wrapping(field: str) -> None:
    changes: dict[str, Any] = {field: MAX_VERSION}
    with pytest.raises(LeaseRejectedError, match="reconciliation_required"):
        decide(accounting=replace(LEDGER, **changes))


@pytest.mark.parametrize("value", [True, -1, 2**53, 1.5, "100"])
def test_invalid_counters_never_reach_database(value: Any) -> None:
    with pytest.raises(ValueError):
        replace(REQUEST, observed_at_us=value)


def test_future_observations_underflow_and_inconsistent_quota_are_not_writes() -> None:
    for change, code in (
        ({"now_us": 99}, "invalid_execution_request"),
        ({"lease": replace(LEASE, lease_id="lease-other")}, "reconciliation_required"),
        ({"lease": replace(LEASE, lease_state="released")}, "lease_not_active"),
        ({"lease": replace(LEASE, version=MAX_VERSION)}, "reconciliation_required"),
        ({"lease": replace(LEASE, budget_released_at_us=1)}, "reconciliation_required"),
        ({"lease": replace(LEASE, cleanup_state="complete")}, "reconciliation_required"),
        (
            {"lease": replace(LEASE, observed_at_us=1, reason_code=REQUEST.reason_code)},
            "reconciliation_required",
        ),
        ({"accounting": replace(LEDGER, held=ZERO)}, "reconciliation_required"),
        ({"accounting": replace(LEDGER, held_slots=0)}, "reconciliation_required"),
        ({"accounting": replace(LEDGER, held_attempts=0)}, "reconciliation_required"),
        ({"accounting": replace(LEDGER, quota_state="released")}, "reconciliation_required"),
        ({"accounting": replace(LEDGER, quota_state="quarantined")}, "reconciliation_required"),
    ):
        with pytest.raises(LeaseRejectedError, match=code):
            decide(**change)
    invalid_changes: tuple[dict[str, Any], ...] = (
        {"observed_at_us": 1},
        {"lease_state": "invalid"},
        {"cleanup_state": "invalid"},
        {"lease_state": "quarantined"},
    )
    for invalid in invalid_changes:
        with pytest.raises(ValueError):
            replace(LEASE, **invalid)
    with pytest.raises(ValueError):
        replace(LEDGER, held_slots=4)
    with pytest.raises(ValueError):
        replace(LEDGER, max_attempts=0)


class Port:
    def quarantine_lease(self, request: QuarantineLease) -> LeaseResult:
        return LeaseResult("committed", request.lease_id, decide().lease)

    def query_lease(self, lease_id: str) -> LeaseResult:
        return LeaseResult("replayed", lease_id, LEASE)


def test_boundary_uses_source_contract_and_never_exposes_reserved_budgets() -> None:
    boundary = LeaseTransactionBoundary(LeaseService(Port()))
    wire = boundary.quarantine_lease(
        {
            "lease_id": REQUEST.lease_id,
            "reason_code": REQUEST.reason_code,
            "observed_at_us": REQUEST.observed_at_us,
        }
    )
    assert wire["value"] == {
        "kind": "lease",
        "lease_id": "lease-test",
        "lease_state": "quarantined",
        "cleanup_state": "quarantined",
        "version": "2",
    }
    assert (
        boundary.query_outcome({"target": {"kind": "lease", "lease_id": "lease-test"}})["value"][
            "version"
        ]
        == "1"
    )
    for request in (
        {"lease_id": "lease-test", "reason_code": "made_up", "observed_at_us": 1},
        {"lease_id": "lease-test", "reason_code": REQUEST.reason_code, "observed_at_us": True},
    ):
        with pytest.raises(ContractValidationError):
            boundary.quarantine_lease(request)
    for query in (
        {"target": {"kind": "job", "job_id": "job-test"}},
        {
            "target": {"kind": "lease", "lease_id": "lease-test"},
            "request_digest": "sha256:" + "a" * 64,
        },
    ):
        with pytest.raises(ContractValidationError):
            boundary.query_outcome(query)
    for result in (
        unknown("lease-test"),
        rejected("lease-test", "execution_cleanup_pending"),
        rejected("lease-test", "idempotency_conflict"),
    ):
        assert boundary._wire("query_outcome", result)["outcome"] == result.outcome
    with pytest.raises(ValueError):
        LeaseResult("committed", "lease-test")
    with pytest.raises(ValueError):
        LeaseResult(
            "unknown", "lease-test", error_code="outcome_unknown", recovery="retry_transaction"
        )


class FaultPool:
    def __init__(self, error: Exception) -> None:
        self.error, self.calls, self.active = error, 0, False

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Any]:
        self.calls += 1
        self.active = True
        error = self.error

        class Connection:
            def execute(self, *args: Any) -> None:
                raise error

            def cursor(self) -> Any:
                raise error

        try:
            yield Connection()
        finally:
            self.active = False


@pytest.mark.parametrize(
    "error", [psycopg.OperationalError("synthetic"), psycopg.InterfaceError("synthetic")]
)
def test_ambiguous_transport_never_repeats_an_isolation_write(error: Exception) -> None:
    pool = FaultPool(error)
    result = PostgresLeaseTransactions(cast(SpeechPool, pool)).quarantine_lease(REQUEST)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    assert pool.calls == 1


@pytest.mark.parametrize(
    "error", [psycopg.errors.DeadlockDetected(), psycopg.errors.SerializationFailure()]
)
def test_confirmed_rollback_retries_only_outside_pool_and_transaction(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool, sleeps = FaultPool(error), []

    def jitter(delay: float) -> None:
        assert not pool.active
        sleeps.append(delay)

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.leases_postgres.time.sleep", jitter
    )
    result = PostgresLeaseTransactions(cast(SpeechPool, pool)).quarantine_lease(REQUEST)
    assert pool.calls == 3 and len(sleeps) == 2 and result.recovery == "retry_transaction"


@pytest.mark.parametrize(
    "error",
    [
        PoolClosed(),
        PoolTimeout(),
        TooManyRequests(),
        _BudgetExpiredError(),
        psycopg.errors.CheckViolation(),
        ValueError("bad row"),
        KeyError("bad row"),
    ],
)
def test_bounded_or_invalid_storage_never_reports_a_commit(error: Exception) -> None:
    pool = FaultPool(error)
    result = PostgresLeaseTransactions(cast(SpeechPool, pool)).query_lease("lease-test")
    assert result.outcome == "rejected" and pool.calls == 1


def test_invalid_adapter_bounds_and_untrusted_reason_fail_before_checkout() -> None:
    for limits in ({"timeout_seconds": 0}, {"timeout_seconds": 31}, {"max_attempts": 4}):
        with pytest.raises(ValueError):
            PostgresLeaseTransactions(cast(SpeechPool, None), **limits)
    with pytest.raises(ValueError):
        PostgresLeaseTransactions(cast(SpeechPool, None)).query_lease("bad/path")
    with pytest.raises(ContractValidationError):
        PostgresLeaseTransactions(cast(SpeechPool, None)).quarantine_lease(
            replace(REQUEST, reason_code="made_up")
        )


def test_expired_statement_budget_prevents_any_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    class Cursor:
        rowcount = 1

        def execute(self, *args: Any) -> None:
            pytest.fail("Expired transaction must not send SQL")

        def fetchall(self) -> list[dict[str, Any]]:
            return [{"now_us": 1}]

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.leases_postgres.time.monotonic", lambda: 10
    )
    cursor = _DeadlineCursor(Cursor(), 9)
    with pytest.raises(_BudgetExpiredError):
        cursor.execute("SELECT 1")


def test_lost_commit_acknowledgement_discards_tentative_success_and_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Cursor:
        rowcount = 1

        def execute(self, *args: Any) -> None:
            pass

        def fetchall(self) -> list[dict[str, Any]]:
            return [{"now_us": 101}]

    class Connection:
        @contextmanager
        def cursor(self) -> Iterator[Any]:
            yield Cursor()

    class CommitLostPool:
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            calls.append("checkout")
            yield Connection()
            # This fault model does not assert actual commit status. It checks
            # the production adapter's ambiguity policy after its work returns.
            raise psycopg.OperationalError("synthetic lost acknowledgement")

    def work(cursor: Any, request: QuarantineLease, *, clock: Any) -> LeaseResult:
        assert clock() == 101 and cursor.rowcount == 1
        calls.append("write")
        return LeaseResult("committed", request.lease_id, decide().lease)

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.leases_postgres.quarantine_in_transaction", work
    )
    result = PostgresLeaseTransactions(cast(SpeechPool, CommitLostPool())).quarantine_lease(REQUEST)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    assert result.fact is None and calls == ["checkout", "write"]


@pytest.mark.parametrize(
    "capacity,used,ppm,state",
    [
        (4, 1, 250000, "active"),
        (4, 2, 250000, "quarantined"),
        (2**53 - 1, 2**53 - 2, 999999, "quarantined"),
        (2**53 - 1, 2**53 - 1, 1000000, "active"),
    ],
)
def test_threshold_is_strict_and_safe_at_largest_resource_counter(
    capacity: int,
    used: int,
    ppm: int,
    state: str,
) -> None:
    vector = PhysicalResources(used, 0, 0, 0, 0, 0)
    ledger = replace(
        LEDGER, capacity=PhysicalResources(capacity, 0, 0, 0, 0, 0), held=vector, threshold_ppm=ppm
    )
    result = decide(lease=replace(LEASE, reserved=vector), accounting=ledger)
    assert result.accounting.node_state == state
