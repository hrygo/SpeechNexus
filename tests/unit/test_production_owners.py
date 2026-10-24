"""Owner policy, wire safety and conservative driver failure classification."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.owners import OwnerService
from speechnexus_backend.speech.domain.owners import (
    MAX_COUNTER,
    MAX_VERSION,
    AcquireOwner,
    ActivateOwner,
    ActivationFacts,
    BarrierFact,
    OwnerAuthority,
    OwnerRejectedError,
    OwnerResult,
    PoolOwnership,
    RenewOwner,
    acquired,
    activated,
    renewed,
    unknown,
)
from speechnexus_backend.speech.infrastructure.owners_postgres import (
    PostgresOwnerTransactions,
    _BudgetExpiredError,
    _DeadlineCursor,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.interfaces.owner_transactions import OwnerTransactionBoundary

AUTHORITY = OwnerAuthority("tts-realtime", "scheduler-test", 1)
INITIAL = PoolOwnership("tts-realtime", None, 0, 0, "reconciling_takeover", 1)
CURRENT = PoolOwnership("tts-realtime", "scheduler-test", 1, 100000, "reconciling_takeover", 2)
DIGEST = "sha256:" + "a" * 64


def test_owner_acquisition_renewal_and_activation_keep_budget_and_epoch_separate() -> None:
    first = acquired(INITIAL, AcquireOwner("tts-realtime", "scheduler-test", 1, 100), 1000)
    assert first.fencing_token == 1 and first.expires_at_us == 101000 and first.version == 2
    refreshed = renewed(first, RenewOwner(AUTHORITY, 2, 100), 2000)
    assert refreshed.fencing_token == first.fencing_token
    assert refreshed.expires_at_us == 102000 and refreshed.version == 3
    evidence = ActivationFacts(
        frozenset({"gateway-test"}), (BarrierFact("gateway-test", 1, DIGEST),), False, False
    )
    active = activated(refreshed, ActivateOwner(AUTHORITY, 3), 3000, evidence)
    assert active.owner_state == "active" and active.version == 4
    assert active.expires_at_us == refreshed.expires_at_us


@pytest.mark.parametrize(
    "authority",
    [
        OwnerAuthority("tts-realtime", "other-scheduler", 1),
        OwnerAuthority("tts-realtime", "scheduler-test", 2),
        OwnerAuthority("asr-realtime", "scheduler-test", 1),
    ],
)
def test_renew_cannot_revive_another_or_fenced_owner(authority: OwnerAuthority) -> None:
    with pytest.raises(OwnerRejectedError, match="stale_fencing_token"):
        renewed(CURRENT, RenewOwner(authority, 2, 100), 1000)


@pytest.mark.parametrize(
    "evidence",
    [
        ActivationFacts(
            frozenset({"gateway-test"}), (BarrierFact("gateway-test", 1, DIGEST),), False, True
        ),
        ActivationFacts(frozenset({"gateway-test"}), (), False, False),
        ActivationFacts(
            frozenset({"gateway-test", "gateway-other"}),
            (BarrierFact("gateway-test", 1, DIGEST),),
            False,
            False,
        ),
        ActivationFacts(
            frozenset({"gateway-test"}), (BarrierFact("gateway-test", 1, DIGEST),), True, False
        ),
    ],
)
def test_activation_requires_every_affected_gateway_and_resolved_dispatch(
    evidence: ActivationFacts,
) -> None:
    with pytest.raises(OwnerRejectedError, match="reconciliation_required"):
        activated(CURRENT, ActivateOwner(AUTHORITY, 2), 1000, evidence)


def test_owner_versions_expiry_and_counter_overflow_cannot_silently_wrap() -> None:
    with pytest.raises(OwnerRejectedError, match="owner_busy"):
        acquired(CURRENT, AcquireOwner("tts-realtime", "other-scheduler", 2, 100), 1000)
    with pytest.raises(OwnerRejectedError, match="version_conflict"):
        acquired(INITIAL, AcquireOwner("tts-realtime", "scheduler-test", 2, 100), 1000)
    with pytest.raises(OwnerRejectedError, match="stale_fencing_token"):
        renewed(CURRENT, RenewOwner(AUTHORITY, 2, 100), CURRENT.expires_at_us)
    for state, now in (
        (replace(CURRENT, fencing_token=MAX_COUNTER, expires_at_us=0), 1000),
        (replace(CURRENT, version=MAX_VERSION, expires_at_us=0), 1000),
        (INITIAL, MAX_COUNTER),
        (INITIAL, -1),
    ):
        with pytest.raises(OwnerRejectedError, match="invalid_counter"):
            acquired(state, AcquireOwner("tts-realtime", "scheduler-test", state.version, 100), now)
    with pytest.raises(OwnerRejectedError, match="invalid_counter"):
        renewed(CURRENT, RenewOwner(AUTHORITY, 2, 60000), -1)


@pytest.mark.parametrize("value", [True, 0, -1, 60001, 1.5, "100"])
def test_domain_rejects_invalid_ttl_types_and_ranges(value: Any) -> None:
    with pytest.raises(ValueError):
        AcquireOwner("tts-realtime", "scheduler-test", 1, value)


def test_success_snapshots_and_unowned_rows_cannot_misrepresent_authority() -> None:
    for values in (
        ("tts-realtime", None, 1, 0, "reconciling_takeover", 1),
        ("tts-realtime", "scheduler-test", 0, 0, "active", 1),
        ("tts-realtime", None, 0, 0, "active", 1),
    ):
        with pytest.raises(ValueError):
            PoolOwnership(*cast(Any, values))
    with pytest.raises(ValueError):
        OwnerResult("committed", "tts-realtime", INITIAL)
    with pytest.raises(ValueError):
        OwnerResult(
            "unknown", "tts-realtime", error_code="outcome_unknown", recovery="retry_transaction"
        )
    with pytest.raises(ValueError):
        BarrierFact("gateway-test", 1, "synthetic-invalid-digest")


class RecordingPort:
    def __init__(self, result: OwnerResult) -> None:
        self.result = result
        self.calls: list[str] = []
        self.requests: list[Any] = []

    def acquire_owner(self, request: AcquireOwner) -> OwnerResult:
        self.calls.append("acquire")
        self.requests.append(request)
        return self.result

    def renew_owner(self, request: RenewOwner) -> OwnerResult:
        self.calls.append("renew")
        self.requests.append(request)
        return self.result

    def activate_owner(self, request: ActivateOwner) -> OwnerResult:
        self.calls.append("activate")
        self.requests.append(request)
        return self.result

    def query_owner(self, pool_id: str) -> OwnerResult:
        self.calls.append("query")
        return self.result


def test_validated_boundary_preserves_dtos_and_never_retries_an_unknown_mutation() -> None:
    port = RecordingPort(unknown("tts-realtime"))
    boundary = OwnerTransactionBoundary(OwnerService(port))
    request = {
        "pool_id": "tts-realtime",
        "owner_id": "scheduler-test",
        "expected_version": "1",
        "ttl_ms": 100.0,
    }
    original = copy.deepcopy(request)
    result = boundary.acquire_owner(request)
    assert result["recovery"] == "query_current_authority"
    assert port.calls == ["acquire"] and request == original
    assert type(port.requests[0].ttl_ms) is int
    request["ttl_ms"] = "100"
    with pytest.raises(ContractValidationError):
        boundary.acquire_owner(request)
    assert port.calls == ["acquire"]


def test_owner_boundary_publishes_complete_versioned_snapshots_and_scopes_queries() -> None:
    port = RecordingPort(OwnerResult("committed", "tts-realtime", CURRENT))
    boundary = OwnerTransactionBoundary(OwnerService(port))
    authority = {"pool_id": "tts-realtime", "owner_id": "scheduler-test", "fencing_token": 1}
    for operation, request in (
        ("renew_owner", {"authority": authority, "expected_version": "2", "ttl_ms": 100}),
        ("activate_owner", {"authority": authority, "expected_version": "2"}),
        ("query_outcome", {"target": {"kind": "pool", "pool_id": "tts-realtime"}}),
    ):
        result = getattr(boundary, operation)(request)
        assert result["operation"] == operation
        assert result["version"] == result["value"]["version"] == "2"
        assert result["value"]["authority"] == authority
    with pytest.raises(ContractValidationError, match="only queries pool authority"):
        boundary.query_outcome({"target": {"kind": "job", "job_id": "job-test"}})
    with pytest.raises(ContractValidationError, match=r"^Invalid acquire_owner$"):
        boundary.acquire_owner(
            {
                "pool_id": "tts-realtime",
                "owner_id": "scheduler-test",
                "expected_version": str(10**19 - 1),
                "ttl_ms": 100,
            }
        )


def test_internal_service_failure_is_not_reported_as_invalid_request() -> None:
    class FailingPort(RecordingPort):
        def acquire_owner(self, request: AcquireOwner) -> OwnerResult:
            raise ValueError("synthetic internal failure")

    boundary = OwnerTransactionBoundary(OwnerService(FailingPort(unknown("tts-realtime"))))
    with pytest.raises(ValueError, match="synthetic internal failure") as failure:
        boundary.acquire_owner(
            {
                "pool_id": "tts-realtime",
                "owner_id": "scheduler-test",
                "expected_version": "1",
                "ttl_ms": 100,
            }
        )
    assert type(failure.value) is ValueError


class FaultPool:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0
        self.active = False

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
def test_transport_ambiguity_never_automatically_reacquires_owner(error: Exception) -> None:
    pool = FaultPool(error)
    port = PostgresOwnerTransactions(cast(SpeechPool, pool))
    result = port.acquire_owner(AcquireOwner("tts-realtime", "scheduler-test", 1, 100))
    assert result.outcome == "unknown" and result.recovery == "query_current_authority"
    assert pool.calls == 1


@pytest.mark.parametrize(
    "error", [psycopg.errors.DeadlockDetected(), psycopg.errors.SerializationFailure()]
)
def test_only_confirmed_aborts_retry_with_jitter_after_connection_release(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = FaultPool(error)
    sleeps: list[float] = []

    def jitter(delay: float) -> None:
        assert not pool.active
        sleeps.append(delay)

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.owners_postgres.time.sleep", jitter
    )
    result = PostgresOwnerTransactions(cast(SpeechPool, pool)).query_owner("tts-realtime")
    assert pool.calls == 3 and len(sleeps) == 2
    assert result.recovery == "retry_transaction"


@pytest.mark.parametrize(
    "error", [PoolTimeout(), PoolClosed(), TooManyRequests(), psycopg.errors.CheckViolation()]
)
def test_queue_or_nonretryable_errors_do_not_loop(error: Exception) -> None:
    pool = FaultPool(error)
    result = PostgresOwnerTransactions(cast(SpeechPool, pool)).query_owner("tts-realtime")
    assert result.outcome == "rejected" and pool.calls == 1


def test_each_statement_timeout_uses_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    class Cursor:
        rowcount = 1

        def __init__(self) -> None:
            self.queries: list[tuple[str, tuple[object, ...]]] = []

        def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
            self.queries.append((query, parameters))

        def fetchall(self) -> list[dict[str, Any]]:
            return [{"now_us": 1234}]

    now = [2.0]
    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.owners_postgres.time.monotonic", lambda: now[0]
    )
    raw = Cursor()
    cursor = _DeadlineCursor(raw, 3.0)
    assert cursor.clock() == 1234
    now[0] = 2.75
    cursor.execute("SELECT 1")
    assert raw.queries[0][1] == ("1000",)
    assert raw.queries[2][1] == ("250",)
    assert cursor.rowcount == 1
    now[0] = 3
    with pytest.raises(_BudgetExpiredError):
        cursor.execute("SELECT 2")


def test_owner_adapter_configuration_is_bounded() -> None:
    for timeout, attempts in ((0, 3), (31, 3), (3, 0), (3, 4)):
        with pytest.raises(ValueError):
            PostgresOwnerTransactions(
                cast(SpeechPool, None), timeout_seconds=timeout, max_attempts=attempts
            )
    with pytest.raises(ValueError):
        PostgresOwnerTransactions(cast(SpeechPool, None)).query_owner("unknown-pool")


def test_pool_queue_time_can_exhaust_budget_before_any_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]

    class DelayedPool:
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            assert timeout == 1
            now[0] = 2
            yield object()  # Any access would fail: no statement is allowed now.

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.owners_postgres.time.monotonic", lambda: now[0]
    )
    result = PostgresOwnerTransactions(
        cast(SpeechPool, DelayedPool()), timeout_seconds=1
    ).query_owner("tts-realtime")
    assert result.outcome == "rejected" and result.recovery == "retry_transaction"
