"""The abort receipt and its bounded attempt, on PostgreSQL and on InnoDB.

PostgreSQL runs the production ledger through a restricted gateway pool. InnoDB runs the
same production statements and pure rules through test-only DB-API plumbing. The
scenarios are shared on purpose: a receipt that can only be recorded, leased or given up
on one engine would be a false oracle for the other.

This is not a product MySQL adapter, and it proves no engine stop, release or HA.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import psycopg
import pymysql
import pytest
from tests.fake.execution import ScriptedEngine, fake_ingress
from tests.fake.execution_command import command_example
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_stop_evidence import new_pool
from tests.integration.test_gateway_submit_ledger import (
    identity_for,
    seed_second_attempt,
)
from tests.integration.test_production_jobs import database as database
from tests.support.gateway_database import MySQLSubmissionLedger

from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    gateway_abort_grants,
)
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.abort_intent import (
    MAX_ABORT_DISPATCH_ATTEMPTS,
    AbortClaimRefusal,
    AbortDispatchClaim,
    AbortDispatchOutcome,
    AbortDispatchResult,
    AbortExecutionRequest,
)
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.ports import (
    AbortRecoveryQueue,
    DurableAbortLedger,
    SubmissionLedger,
)
from speechnexus_gateway.domain.recovery import ExecutionObservationConflictError
from speechnexus_gateway.domain.submission import SubmitRequest
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

pytestmark = pytest.mark.integration
INTENT_ID = "intent-a"
LEASE = "lease-alt-stage-main"
#: MySQL ER_CHECK_CONSTRAINT_VIOLATED; PostgreSQL raises psycopg's CheckViolation.
_MYSQL_CHECK_VIOLATION = 3819


class AbortLedgerOracle(DurableAbortLedger, AbortRecoveryQueue, SubmissionLedger, Protocol):
    """The receipt surface plus the ordinary facts and the due queue the scenarios read."""


@pytest.fixture
def abort_ledger(foundation: Foundation) -> Iterator[AbortLedgerOracle]:
    db = foundation.database
    if db.dialect == "mysql":
        yield MySQLSubmissionLedger(foundation)
        return
    for grant in gateway_abort_grants(db.schema, foundation.gateway_role):
        db.query(grant)
    pool = new_pool(foundation)
    try:
        yield PostgresSubmissionLedger(pool, timeout_seconds=5)
    finally:
        pool.close()


def received_abort(foundation: Foundation, ledger: AbortLedgerOracle) -> AbortExecutionRequest:
    """Create one executing intent through the ledger, then its original receipt."""
    seed_second_attempt(foundation)
    command = cast(JsonValue, command_example(0))
    identity = replace(
        identity_for(LEASE, submit_intent_id=INTENT_ID), command_digest=command_digest(command)
    )
    engine = ScriptedEngine()
    SubmitExecutionService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=ledger,
        dispatch=engine,
        observation=engine,
    ).submit(SubmitRequest(identity=identity, command=command, output_stream_id="tts-main"))
    request = AbortExecutionRequest(
        INTENT_ID, "operation-test", "attempt-alt", 1, "client_cancelled", 1000
    )
    ledger.receive_abort(request)
    return request


def receipt(foundation: Foundation) -> dict[str, Any]:
    return foundation.database.query(
        "SELECT submit_intent_id, instance_id, instance_generation, received_at_us, "
        "dispatched_at_us, dispatch_attempt_id, dispatch_started_at_us, "
        "dispatch_deadline_at_us, dispatch_outcome, dispatch_outcome_at_us, "
        "dispatch_attempts, next_check_at_us FROM gateway_abort_intents "
        "WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )[0]


def original_origin(
    foundation: Foundation, request: AbortExecutionRequest
) -> AbortExecutionRequest:
    """The recorded origin recovery must re-verify before any resend."""
    row = receipt(foundation)
    return replace(
        request, instance_id=row["instance_id"], instance_generation=row["instance_generation"]
    )


def db_now_us(foundation: Foundation) -> int:
    clock = (
        "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        if foundation.database.dialect == "postgresql"
        else "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000 AS SIGNED) AS now_us"
    )
    return int(foundation.database.query(clock)[0]["now_us"])


def expire_lease(foundation: Foundation) -> None:
    """Make the recorded lease due without waiting for real time."""
    foundation.database.query(
        "UPDATE gateway_abort_intents SET dispatch_deadline_at_us=dispatch_started_at_us + 1, "
        "next_check_at_us=0 WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )


def claimed(value: AbortDispatchClaim | AbortClaimRefusal) -> AbortDispatchClaim:
    assert isinstance(value, AbortDispatchClaim)
    return value


def test_receive_records_the_immutable_origin_and_replays_without_rewriting(
    foundation: Foundation, abort_ledger: AbortLedgerOracle
) -> None:
    request = received_abort(foundation, abort_ledger)
    first = receipt(foundation)
    identity = abort_ledger.get(INTENT_ID)
    assert identity is not None
    assert (first["instance_id"], first["instance_generation"]) == (
        identity.identity.instance_id,
        identity.identity.instance_generation,
    )
    assert first["received_at_us"] > 0 and first["dispatch_attempts"] == 0
    abort_ledger.receive_abort(request)
    assert receipt(foundation) == first
    for changed in (
        replace(request, reason="deadline_exceeded"),
        replace(request, wait_timeout_ms=2000),
    ):
        with pytest.raises(ExecutionObservationConflictError):
            abort_ledger.receive_abort(changed)
    assert receipt(foundation) == first


def test_one_live_lease_allows_exactly_one_sender(
    foundation: Foundation, abort_ledger: AbortLedgerOracle
) -> None:
    request = received_abort(foundation, abort_ledger)
    first = claimed(abort_ledger.claim_abort(request, attempt_id="abort-1", lease_ms=2000))
    assert first.attempts == 1
    assert abort_ledger.claim_abort(request, attempt_id="abort-2", lease_ms=2000) is (
        AbortClaimRefusal.LEASE_HELD
    )
    assert receipt(foundation)["dispatch_attempts"] == 1
    expire_lease(foundation)
    second = claimed(abort_ledger.claim_abort(request, attempt_id="abort-2", lease_ms=2000))
    assert second.attempts == 2 and second.attempt_id == "abort-2"
    assert receipt(foundation)["dispatch_attempts"] == 2


def test_undecided_outcome_schedules_the_retry_from_the_database_clock(
    foundation: Foundation, abort_ledger: AbortLedgerOracle
) -> None:
    """The adapter observation is an audit fact; only the database clock schedules."""
    request = received_abort(foundation, abort_ledger)
    claimed(abort_ledger.claim_abort(request, attempt_id="abort-1", lease_ms=2000))
    hour_old = datetime(2020, 1, 1, tzinfo=UTC)
    assert abort_ledger.record_abort_dispatch(
        AbortDispatchResult(
            submit_intent_id=INTENT_ID,
            attempt_id="abort-1",
            outcome=AbortDispatchOutcome.UNCONFIRMED,
            observed_at=hour_old,
            retry_delay_ms=30_000,
        )
    )
    row = receipt(foundation)
    assert row["dispatch_outcome"] == "unconfirmed"
    assert row["dispatch_outcome_at_us"] == int(hour_old.timestamp() * 1_000_000)
    assert 29_000_000 <= row["next_check_at_us"] - db_now_us(foundation) <= 30_000_000
    expire_lease(foundation)
    claimed(abort_ledger.claim_abort(request, attempt_id="abort-2", lease_ms=2000))
    assert abort_ledger.record_abort_dispatch(
        AbortDispatchResult(
            submit_intent_id=INTENT_ID,
            attempt_id="abort-2",
            outcome=AbortDispatchOutcome.UNCONFIRMED,
            observed_at=datetime.now(UTC),
            retry_delay_ms=60_000,
        )
    )
    assert (
        58_000_000
        <= receipt(foundation)["next_check_at_us"] - db_now_us(foundation)
        <= (60_000_000)
    )
    # An outcome is written for the attempt that produced it, exactly once.
    assert not abort_ledger.record_abort_dispatch(
        AbortDispatchResult(
            submit_intent_id=INTENT_ID,
            attempt_id="abort-2",
            outcome=AbortDispatchOutcome.ACKNOWLEDGED,
            observed_at=datetime.now(UTC),
        )
    )


def test_decided_outcome_ends_the_receipt(
    foundation: Foundation, abort_ledger: AbortLedgerOracle
) -> None:
    request = received_abort(foundation, abort_ledger)
    claimed(abort_ledger.claim_abort(request, attempt_id="abort-1", lease_ms=2000))
    assert abort_ledger.record_abort_dispatch(
        AbortDispatchResult(
            submit_intent_id=INTENT_ID,
            attempt_id="abort-1",
            outcome=AbortDispatchOutcome.ACKNOWLEDGED,
            observed_at=datetime.now(UTC),
        )
    )
    expire_lease(foundation)
    assert abort_ledger.claim_abort(request, attempt_id="abort-2", lease_ms=2000) is (
        AbortClaimRefusal.DECIDED
    )
    assert receipt(foundation)["dispatch_attempts"] == 1


def test_spent_attempts_are_given_up_once_after_the_last_crash(
    foundation: Foundation, abort_ledger: AbortLedgerOracle
) -> None:
    """The last attempt died before recording; the next claim records the give-up."""
    request = received_abort(foundation, abort_ledger)
    for attempt in range(1, MAX_ABORT_DISPATCH_ATTEMPTS + 1):
        claimed(abort_ledger.claim_abort(request, attempt_id=f"abort-{attempt}", lease_ms=2000))
        expire_lease(foundation)
    assert receipt(foundation)["dispatch_attempts"] == MAX_ABORT_DISPATCH_ATTEMPTS
    assert abort_ledger.claim_abort(request, attempt_id="abort-last", lease_ms=2000) is (
        AbortClaimRefusal.EXHAUSTED
    )
    row = receipt(foundation)
    assert row["dispatch_outcome"] == "exhausted"
    assert row["dispatch_attempts"] == MAX_ABORT_DISPATCH_ATTEMPTS
    assert row["dispatch_outcome_at_us"] is not None
    # The give-up is terminal: no later caller gets authority again.
    expire_lease(foundation)
    assert abort_ledger.claim_abort(request, attempt_id="abort-after", lease_ms=2000) is (
        AbortClaimRefusal.DECIDED
    )


def test_due_queue_returns_the_original_origin_and_steps_its_check(
    foundation: Foundation, abort_ledger: AbortLedgerOracle
) -> None:
    request = received_abort(foundation, abort_ledger)
    due = abort_ledger.take_due_aborts(gateway_id="gateway-test", host_node_id="host-test", limit=1)
    # The due receipt carries the origin recorded at receive time, not a caller guess.
    assert due == [original_origin(foundation, request)]
    # Taking it only advances the schedule: the same receipt is not immediately due.
    assert (
        abort_ledger.take_due_aborts(gateway_id="gateway-test", host_node_id="host-test", limit=1)
        == []
    )
    # A live attempt lease also keeps the receipt out of the due queue.
    expire_lease(foundation)
    claimed(abort_ledger.claim_abort(request, attempt_id="abort-1", lease_ms=2000))
    assert (
        abort_ledger.take_due_aborts(gateway_id="gateway-test", host_node_id="host-test", limit=1)
        == []
    )
    expire_lease(foundation)
    assert abort_ledger.take_due_aborts(
        gateway_id="gateway-test", host_node_id="host-test", limit=1
    ) == [original_origin(foundation, request)]


def test_a_receipt_that_no_longer_names_the_authority_is_a_conflict(
    foundation: Foundation, abort_ledger: AbortLedgerOracle
) -> None:
    """Identity is written once at admission; a disagreement never becomes a send."""
    request = received_abort(foundation, abort_ledger)
    foundation.database.query(
        "UPDATE gateway_abort_intents SET instance_generation=instance_generation + 1 "
        "WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )
    with pytest.raises(ExecutionObservationConflictError):
        abort_ledger.claim_abort(request, attempt_id="abort-drift", lease_ms=2000)
    row = receipt(foundation)
    assert row["dispatch_outcome"] is None and row["dispatch_attempts"] == 0
    assert row["dispatch_attempt_id"] is None


def constraint_violated(
    foundation: Foundation, statement: str, parameters: tuple[Any, ...]
) -> None:
    """Prove the database rejects a receipt fact, not merely that it parsed the DDL."""
    try:
        foundation.database.query(statement, parameters)
    except psycopg.errors.CheckViolation:
        return
    except pymysql.err.OperationalError as error:
        # ER_CHECK_CONSTRAINT_VIOLATED; a parse, privilege or lock error is not this.
        assert error.args and error.args[0] == _MYSQL_CHECK_VIOLATION, error
        return
    pytest.fail("the database accepted a receipt fact its constraints forbid")


def test_the_receipt_rejects_a_broken_attempt_and_a_dispatch_before_receipt(
    foundation: Foundation, abort_ledger: AbortLedgerOracle
) -> None:
    """Both engines enforce the DDL invariants instead of only accepting the table."""
    received_abort(foundation, abort_ledger)
    before = receipt(foundation)
    # First dispatch is still pending, so the cross-column check compares against 0.
    assert before["dispatched_at_us"] is None
    # The durable vocabulary has no fenced refusal: the column takes three values.
    constraint_violated(
        foundation,
        "UPDATE gateway_abort_intents SET dispatch_attempt_id='abort-invalid', "
        "dispatch_started_at_us=1, dispatch_deadline_at_us=2, "
        "dispatch_outcome='rejected_fenced', dispatch_outcome_at_us=1 "
        "WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )
    # An outcome without its attempt is not a half-written attempt group.
    constraint_violated(
        foundation,
        "UPDATE gateway_abort_intents SET dispatch_outcome='acknowledged', "
        "dispatch_outcome_at_us=1 WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )
    # The cross-column invariant MySQL can only hold as a named table constraint.
    constraint_violated(
        foundation,
        "UPDATE gateway_abort_intents SET dispatched_at_us=0 WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )
    assert receipt(foundation) == before


def test_the_gateway_role_cannot_rewrite_the_recorded_identity(
    foundation: Foundation, abort_ledger: AbortLedgerOracle
) -> None:
    """The receipt copies a lease-owned identity the gateway may only read."""
    received_abort(foundation, abort_ledger)
    statement = (
        "UPDATE gateway_abort_intents SET instance_generation=instance_generation + 1 "
        "WHERE submit_intent_id=%s"
    )
    with (
        foundation.connection("gateway") as connection,
        connection.cursor() as cursor,
        pytest.raises(
            (psycopg.errors.InsufficientPrivilege, pymysql.err.OperationalError)
        ) as caught,
    ):
        cursor.execute(statement, (INTENT_ID,))
    error = caught.value
    if isinstance(error, pymysql.err.OperationalError):
        # ER_TABLEACCESS_DENIED_ERROR / ER_COLUMNACCESS_DENIED_ERROR.
        assert error.args[0] in (1142, 1143), error
