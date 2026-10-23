"""Submit-ledger failure classification and bounded retries."""

from __future__ import annotations

from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_gateway.domain.submission import (
    SubmissionIntent,
    SubmissionIntentExistsError,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    SubmissionStoreUnavailableError,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger


class FailingPool:
    """Pool double whose every connection attempt raises the same error."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def connection(self, *, timeout: float | None = None) -> Any:
        self.calls += 1
        raise self.error


def ledger_for(
    error: Exception, *, max_attempts: int = 3
) -> tuple[PostgresSubmissionLedger, FailingPool]:
    pool = FailingPool(error)
    return (
        PostgresSubmissionLedger(cast(GatewayPool, pool), max_attempts=max_attempts),
        pool,
    )


def test_a_lost_connection_is_an_unknown_outcome_and_is_not_retried() -> None:
    ledger, pool = ledger_for(psycopg.OperationalError("connection lost"))

    with pytest.raises(SubmissionOutcomeUnknownError) as raised:
        ledger.get("intent-1")

    assert raised.value.code == "outcome_unknown"
    assert raised.value.recovery == "query_original"
    assert pool.calls == 1


def test_a_lost_interface_is_also_never_reported_as_a_clean_failure() -> None:
    ledger, pool = ledger_for(psycopg.InterfaceError("interface closed"))

    with pytest.raises(SubmissionOutcomeUnknownError):
        ledger.add(cast(SubmissionIntent, None), request_document="{}")

    assert pool.calls == 1


@pytest.mark.parametrize(
    "error",
    [
        PoolTimeout("no connection in time"),
        PoolClosed(),
        TooManyRequests("queue full"),
    ],
)
def test_a_pool_that_never_handed_out_a_connection_stays_retryable(error: Exception) -> None:
    ledger, pool = ledger_for(error)

    with pytest.raises(SubmissionStoreUnavailableError) as raised:
        ledger.get("intent-1")

    assert raised.value.code == "reconciliation_required"
    assert raised.value.recovery == "retry_transaction"
    assert pool.calls == 1


def test_a_confirmed_abort_is_retried_within_three_attempts() -> None:
    ledger, pool = ledger_for(psycopg.errors.DeadlockDetected("deadlock detected"))

    with pytest.raises(SubmissionStoreUnavailableError):
        ledger.get("intent-1")

    assert pool.calls == 3


def test_a_serialization_failure_is_retried_once_when_attempts_allow() -> None:
    ledger, pool = ledger_for(psycopg.errors.SerializationFailure("retry"), max_attempts=1)

    with pytest.raises(SubmissionStoreUnavailableError):
        ledger.get("intent-1")

    assert pool.calls == 1


def test_an_existing_intent_is_a_conflict_rather_than_a_second_write() -> None:
    ledger, pool = ledger_for(psycopg.errors.UniqueViolation("duplicate key"))

    with pytest.raises(SubmissionIntentExistsError) as raised:
        ledger.add(cast(SubmissionIntent, None), request_document="{}")

    assert raised.value.code == "idempotency_conflict"
    assert pool.calls == 1


def test_any_other_database_error_requires_reconciliation() -> None:
    ledger, pool = ledger_for(psycopg.errors.InsufficientPrivilege("permission denied"))

    with pytest.raises(SubmissionStoreError) as raised:
        ledger.get("intent-1")

    assert raised.value.code == "reconciliation_required"
    assert raised.value.recovery == "reconcile"
    assert pool.calls == 1


@pytest.mark.parametrize(
    ("timeout_seconds", "max_attempts"),
    [(0.0, 3), (31.0, 3), (3.0, 0), (3.0, 4)],
)
def test_the_adapter_refuses_unbounded_deadlines_or_retries(
    timeout_seconds: float, max_attempts: int
) -> None:
    pool = FailingPool(psycopg.OperationalError("unused"))

    with pytest.raises(ValueError, match="bounded"):
        PostgresSubmissionLedger(
            cast(GatewayPool, pool), timeout_seconds=timeout_seconds, max_attempts=max_attempts
        )
