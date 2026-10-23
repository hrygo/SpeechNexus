"""Claim fencing, failure classification and source-contract boundary checks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.claims import ClaimService
from speechnexus_backend.speech.domain.claims import (
    MAX_COUNTER,
    ClaimJob,
    ClaimRejectedError,
    ClaimResult,
    JobClaim,
    claimed,
)
from speechnexus_backend.speech.infrastructure.claims_postgres import (
    PostgresClaimTransactions,
    _BudgetExpiredError,
    _DeadlineCursor,
)
from speechnexus_backend.speech.infrastructure.claims_sql import (
    claim_in_transaction,
    query_in_transaction,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.interfaces.claim_transactions import ClaimTransactionBoundary

INITIAL = JobClaim("job-test", None, 0, 0, "pending")
REQUEST = ClaimJob("job-test", "worker-test", 0, 100)
CURRENT = claimed(INITIAL, REQUEST, 1000)


def test_current_version_and_expiry_fence_reclaims_even_for_same_worker() -> None:
    assert CURRENT.claim_version == 1 and CURRENT.expires_at_us == 101000
    for request, now in (
        (REQUEST, 101000),
        (replace(REQUEST, expected_claim_version=1), 100999),
        (replace(REQUEST, worker_id="worker-other", expected_claim_version=1), 100999),
    ):
        with pytest.raises(ClaimRejectedError, match="version_conflict"):
            claimed(CURRENT, request, now)
    updated = claimed(CURRENT, replace(REQUEST, expected_claim_version=1), 101000)
    assert updated.claim_version == 2 and updated.expires_at_us == 201000
    with pytest.raises(ClaimRejectedError, match="version_conflict"):
        claimed(
            replace(CURRENT, job_state="completed"),
            replace(REQUEST, expected_claim_version=1),
            101000,
        )


@pytest.mark.parametrize("value", [True, -1, 1.5, "1", MAX_COUNTER + 1])
def test_direct_claim_boundary_rejects_non_integer_or_unsafe_versions(value: Any) -> None:
    with pytest.raises(ValueError):
        replace(REQUEST, expected_claim_version=value)


@pytest.mark.parametrize("value", [True, 0, -1, 60001, 1.5, "1"])
def test_direct_claim_boundary_rejects_invalid_ttl(value: Any) -> None:
    with pytest.raises(ValueError):
        replace(REQUEST, ttl_ms=value)


def test_counter_overflow_and_corrupt_authorities_cannot_be_published() -> None:
    for state, command, now in (
        (INITIAL, REQUEST, -1),
        (INITIAL, REQUEST, MAX_COUNTER),
        (
            replace(CURRENT, claim_version=MAX_COUNTER),
            replace(REQUEST, expected_claim_version=MAX_COUNTER),
            101000,
        ),
    ):
        with pytest.raises(ClaimRejectedError, match="invalid_counter"):
            claimed(state, command, now)
    with pytest.raises(ValueError):
        replace(INITIAL, claim_version=1)
    with pytest.raises(ValueError):
        ClaimResult("committed", "job-test", INITIAL)
    with pytest.raises(ValueError):
        ClaimResult(
            "unknown", "job-test", error_code="outcome_unknown", recovery="retry_transaction"
        )


class Cursor:
    def __init__(self, row: dict[str, Any] | None, *, changed: int = 1) -> None:
        self.row = row
        self.rowcount = changed
        self.queries: list[str] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.queries.append(query)

    def fetchall(self) -> list[dict[str, Any]]:
        return [] if self.row is None else [self.row]


def test_lock_precedes_clock_and_failed_conditional_update_rejects() -> None:
    cursor = Cursor(
        {
            "job_id": "job-test",
            "worker_id": None,
            "claim_version": 0,
            "expires_at_us": 0,
            "job_state": "pending",
        },
        changed=0,
    )

    def clock() -> int:
        assert cursor.queries[-1].endswith("FOR UPDATE")
        return 1000

    with pytest.raises(ClaimRejectedError, match="reconciliation_required"):
        claim_in_transaction(cursor, REQUEST, clock=clock)
    assert len(cursor.queries) == 2


def test_missing_unclaimed_and_corrupt_rows_do_not_invent_claim_authority() -> None:
    with pytest.raises(ClaimRejectedError, match="not_found"):
        query_in_transaction(Cursor(None), "job-test")
    with pytest.raises(ClaimRejectedError, match="reconciliation_required"):
        query_in_transaction(Cursor({"job_id": "job-test"}), "job-test")
    result = query_in_transaction(
        Cursor(
            {
                "job_id": "job-test",
                "worker_id": None,
                "claim_version": 0,
                "expires_at_us": 0,
                "job_state": "pending",
            }
        ),
        "job-test",
    )
    assert result.outcome == "unknown" and result.recovery == "query_current_authority"


class FixedPort:
    def __init__(self) -> None:
        self.commands: list[ClaimJob] = []

    def claim_job(self, request: ClaimJob) -> ClaimResult:
        self.commands.append(request)
        return ClaimResult("committed", request.job_id, CURRENT)

    def query_claim(self, job_id: str) -> ClaimResult:
        return ClaimResult("replayed", job_id, CURRENT)


def test_machine_boundary_validates_without_defaults_or_mutating_input() -> None:
    port = FixedPort()
    boundary = ClaimTransactionBoundary(ClaimService(port))
    request = {
        "job_id": "job-test",
        "worker_id": "worker-test",
        "expected_claim_version": 0,
        "ttl_ms": 100.0,
    }
    assert boundary.claim_job(request)["value"]["claim_version"] == 1
    assert isinstance(request["ttl_ms"], float)
    assert type(port.commands[0].ttl_ms) is int
    wire = boundary.query_outcome({"target": {"kind": "job", "job_id": "job-test"}})
    assert wire["value"]["worker_id"] == "worker-test"
    for invalid in (
        {**request, "ttl_ms": "100"},
        {**request, "expected_claim_version": True},
        {**request, "extra": 1},
    ):
        with pytest.raises(ContractValidationError):
            boundary.claim_job(invalid)
    with pytest.raises(ContractValidationError):
        boundary.query_outcome({"target": {"kind": "pool", "pool_id": "tts-realtime"}})
    assert len(port.commands) == 1


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
def test_transport_ambiguity_never_reclaims_automatically(error: Exception) -> None:
    pool = FaultPool(error)
    result = PostgresClaimTransactions(cast(SpeechPool, pool)).claim_job(REQUEST)
    assert result.outcome == "unknown" and result.recovery == "query_current_authority"
    assert pool.calls == 1


@pytest.mark.parametrize(
    "error", [psycopg.errors.DeadlockDetected(), psycopg.errors.SerializationFailure()]
)
def test_confirmed_abort_retries_after_releasing_connection(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = FaultPool(error)
    sleeps: list[float] = []

    def jitter(delay: float) -> None:
        assert not pool.active
        sleeps.append(delay)

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.claims_postgres.time.sleep", jitter
    )
    result = PostgresClaimTransactions(cast(SpeechPool, pool)).claim_job(REQUEST)
    assert pool.calls == 3 and len(sleeps) == 2
    assert result.recovery == "retry_transaction"


@pytest.mark.parametrize("error", [PoolClosed(), PoolTimeout(), TooManyRequests()])
def test_pool_backpressure_does_not_create_authority(error: Exception) -> None:
    pool = FaultPool(error)
    result = PostgresClaimTransactions(cast(SpeechPool, pool)).claim_job(REQUEST)
    assert result.outcome == "rejected" and result.recovery == "retry_transaction"
    assert pool.calls == 1


def test_expired_pool_queue_budget_prevents_any_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]

    class DelayedPool:
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            now[0] = 2
            yield object()

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.claims_postgres.time.monotonic", lambda: now[0]
    )
    result = PostgresClaimTransactions(
        cast(SpeechPool, DelayedPool()), timeout_seconds=1
    ).query_claim("job-test")
    assert result.outcome == "rejected" and result.recovery == "retry_transaction"


def test_adapter_rejects_unbounded_configuration_and_invalid_query_identity() -> None:
    for timeout, attempts in ((0, 3), (31, 3), (3, 0), (3, 4)):
        with pytest.raises(ValueError):
            PostgresClaimTransactions(
                cast(SpeechPool, None), timeout_seconds=timeout, max_attempts=attempts
            )
    with pytest.raises(ValueError):
        PostgresClaimTransactions(cast(SpeechPool, None)).query_claim("bad/path")


def test_each_statement_uses_remaining_budget_and_expiry_prevents_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1.0]
    queries: list[tuple[str, tuple[object, ...]]] = []

    class RawCursor:
        rowcount = 1

        def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
            queries.append((query, parameters))

        def fetchall(self) -> list[dict[str, Any]]:
            return [{"now_us": 1000}]

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.claims_postgres.time.monotonic", lambda: now[0]
    )
    cursor = _DeadlineCursor(RawCursor(), 2.0)
    assert cursor.clock() == 1000 and cursor.rowcount == 1
    assert queries[0][1] == ("1000",)
    now[0] = 1.75
    cursor.execute("SELECT 1")
    assert queries[2][1] == ("250",)
    now[0] = 2.0
    with pytest.raises(_BudgetExpiredError):
        cursor.execute("SELECT 2")
    assert len(queries) == 4
