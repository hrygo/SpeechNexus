"""Real PG/InnoDB body erasure with synthetic authorization and A-owned release fixtures."""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from speechnexus_contracts.validation import validate_coordination
from tests.fake.command_deletion import SyntheticCommandDeletionAuthorizer
from tests.fake.execution import ScriptedEngine, fake_ingress
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import (
    counters,
    evidence_for,
    new_pool,
    start_execution,
)
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.application.redact_execution_command import (
    ExecutionCommandRedactionService,
)
from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    CommandRedactionConflictError,
    RedactExecutionCommand,
)
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    InvalidExecutionRequestError,
    StaleSubmissionVersionError,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.interfaces.command_redaction_transactions import (
    ExecutionCommandRedactionBoundary,
)

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "d" * 64


def permit_for(intent: SubmissionIntent, **changes: Any) -> AuthorizedCommandRedaction:
    return AuthorizedCommandRedaction(
        replace(
            RedactExecutionCommand(
                intent.identity.submit_intent_id, intent.version, "deletion-test", DIGEST
            ),
            **changes,
        ),
        intent.identity,
    )


def release_fixture(foundation: Foundation) -> None:
    """Synthetic A-owned fact; does not qualify A's production release transaction."""
    foundation.database.query(
        "UPDATE admission_leases SET lease_state = 'released', cleanup_state = 'complete', "
        "budget_released_at_us = 2 WHERE lease_id = 'lease-alt-stage-main'"
    )


def ready(
    foundation: Foundation, ledger: Ledger, *, release: bool = True
) -> tuple[SubmissionIntent, ScriptedEngine]:
    service, engine = start_execution(foundation, ledger)
    intent = service.record_stop(evidence_for(foundation))
    if release:
        release_fixture(foundation)
    return intent, engine


def row(foundation: Foundation) -> dict[str, Any]:
    return foundation.database.query(
        "SELECT * FROM submit_intents WHERE submit_intent_id = 'intent-a'"
    )[0]


def boundary_for(
    ledger: Ledger, permit: AuthorizedCommandRedaction
) -> ExecutionCommandRedactionBoundary:
    return ExecutionCommandRedactionBoundary(
        ExecutionCommandRedactionService(
            ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
            authorizer=SyntheticCommandDeletionAuthorizer(permit),
            ledger=ledger,
        )
    )


def test_body_and_tombstone_commit_together_without_touching_any_other_evidence(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original, engine = ready(foundation, recovery_ledger)
    before = row(foundation)
    budgets = counters(foundation)
    stops = foundation.database.query("SELECT * FROM execution_stop_evidence ORDER BY evidence_id")
    intervals = foundation.database.query(
        "SELECT * FROM output_sample_intervals ORDER BY interval_id"
    )
    permit = permit_for(original)
    first = recovery_ledger.redact_execution_command(permit)
    second = recovery_ledger.redact_execution_command(permit)
    assert first.replayed is False and second.replayed is True
    assert first.intent == second.intent == recovery_ledger.get("intent-a")
    assert first.tombstone == second.tombstone
    assert first.intent.version == original.version + 1
    after = row(foundation)
    allowed = {
        "request_document",
        "command_redacted_at_us",
        "command_deletion_job_id",
        "command_deletion_evidence_digest",
        "updated_at_us",
        "version",
    }
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed
    }
    assert before["request_document"] is not None and after["request_document"] is None
    assert after["command_deletion_job_id"] == "deletion-test"
    assert after["command_deletion_evidence_digest"] == DIGEST
    assert after["command_redacted_at_us"] == after["updated_at_us"]
    assert (
        foundation.database.query("SELECT * FROM execution_stop_evidence ORDER BY evidence_id")
        == stops
    )
    assert (
        foundation.database.query("SELECT * FROM output_sample_intervals ORDER BY interval_id")
        == intervals
    )
    assert counters(foundation) == budgets and engine.dispatch_count == 1
    boundary = boundary_for(recovery_ledger, permit)
    result = boundary.redact_execution_command(
        {
            "submit_intent_id": "intent-a",
            "expected_version": str(original.version),
            "deletion_job_id": "deletion-test",
            "deletion_evidence_digest": DIGEST,
        }
    )
    assert result["outcome"] == "replayed"
    assert result["value"] == {
        "kind": "fact",
        "fact_id": "intent-a",
        "request_digest": original.request_digest,
    }
    validate_coordination("TransactionResult", result)


def test_stop_is_not_enough_until_a_has_released_the_lease(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    intent, _ = ready(foundation, recovery_ledger, release=False)
    before = row(foundation)
    with pytest.raises(ExecutionCleanupPendingError):
        recovery_ledger.redact_execution_command(permit_for(intent))
    assert row(foundation) == before


@pytest.mark.parametrize("unknown", [False, True])
def test_a_released_lease_does_not_allow_erasing_an_active_or_unknown_execution(
    foundation: Foundation, recovery_ledger: Ledger, unknown: bool
) -> None:
    start_execution(foundation, recovery_ledger, unknown=unknown)
    release_fixture(foundation)
    intent = recovery_ledger.get("intent-a")
    assert intent is not None
    before = row(foundation)
    with pytest.raises(ExecutionCleanupPendingError):
        recovery_ledger.redact_execution_command(permit_for(intent))
    assert row(foundation) == before


def test_stale_version_and_a_different_permit_identity_leave_the_body_intact(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original, _ = ready(foundation, recovery_ledger)
    before = row(foundation)
    with pytest.raises(StaleSubmissionVersionError):
        recovery_ledger.redact_execution_command(permit_for(original, expected_version=1))
    permit = permit_for(original)
    wrong = replace(permit, identity=replace(permit.identity, instance_generation=2))
    with pytest.raises(InvalidExecutionRequestError):
        recovery_ledger.redact_execution_command(wrong)
    assert row(foundation) == before


@pytest.mark.parametrize(
    "changed",
    [
        {"deletion_job_id": "deletion-other"},
        {"deletion_evidence_digest": "sha256:" + "a" * 64},
    ],
)
def test_tombstone_is_immutable_even_for_a_newly_authorized_request(
    foundation: Foundation, recovery_ledger: Ledger, changed: dict[str, str]
) -> None:
    original, _ = ready(foundation, recovery_ledger)
    recovery_ledger.redact_execution_command(permit_for(original))
    before = row(foundation)
    with pytest.raises(CommandRedactionConflictError):
        recovery_ledger.redact_execution_command(permit_for(original, **changed))
    assert row(foundation) == before


@pytest.mark.parametrize("different", [False, True])
def test_two_cleanup_writers_publish_one_tombstone_and_one_version(
    foundation: Foundation, recovery_ledger: Ledger, different: bool
) -> None:
    original, _ = ready(foundation, recovery_ledger)
    permit = permit_for(original)
    other = permit_for(original, deletion_job_id="deletion-other") if different else permit

    def write(value: AuthorizedCommandRedaction) -> str:
        try:
            return (
                "replayed"
                if recovery_ledger.redact_execution_command(value).replayed
                else "committed"
            )
        except CommandRedactionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(write, (permit, other)))
    assert sorted(results) == sorted(["committed", "conflict" if different else "replayed"])
    assert row(foundation)["version"] == original.version + 1


def test_constraint_failure_rolls_back_body_tombstone_and_version(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original, _ = ready(foundation, recovery_ledger)
    before = row(foundation)
    foundation.database.query(
        "ALTER TABLE submit_intents ADD CONSTRAINT injected_redaction_failure "
        "CHECK (request_document IS NOT NULL)"
    )
    with pytest.raises(SubmissionStoreError):
        recovery_ledger.redact_execution_command(permit_for(original))
    assert row(foundation) == before


def wait_for_cleanup_lock(foundation: Foundation) -> None:
    db = foundation.database
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if db.dialect == "postgresql":
            rows = db.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = %s "
                "AND cardinality(pg_blocking_pids(pid)) > 0",
                (foundation.gateway_role,),
            )
        else:
            rows = db.query(
                "SELECT count(*) AS n FROM performance_schema.data_lock_waits w JOIN "
                "performance_schema.data_locks l "
                "ON w.REQUESTING_ENGINE_LOCK_ID = l.ENGINE_LOCK_ID WHERE l.OBJECT_SCHEMA = %s",
                (db.schema,),
            )
        if rows[0]["n"]:
            return
        time.sleep(0.01)
    pytest.fail("Cleanup requires an observed real database row-lock wait")


def test_release_is_rechecked_and_time_is_sampled_after_the_lease_lock_wait(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original, _ = ready(foundation, recovery_ledger, release=False)
    permit = permit_for(original)
    with foundation.database.blocker() as blocker:
        with blocker.cursor() as cursor:
            cursor.execute(
                "UPDATE admission_leases SET lease_state = 'released', cleanup_state = 'complete', "
                "budget_released_at_us = 2 WHERE lease_id = 'lease-alt-stage-main'"
            )
        with ThreadPoolExecutor(max_workers=1) as workers:
            waiting = workers.submit(recovery_ledger.redact_execution_command, permit)
            try:
                wait_for_cleanup_lock(foundation)
                assert row(foundation)["request_document"] is not None
                released_at = datetime.now(UTC)
                blocker.commit()
            finally:
                blocker.rollback()
            written = waiting.result(timeout=5)
            assert written.tombstone.redacted_at >= released_at


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_actual_commit_with_a_lost_ack_is_queried_without_automatic_retry(
    foundation: Foundation,
) -> None:
    pool = new_pool(foundation)
    try:
        ledger = PostgresSubmissionLedger(pool)
        original, _ = ready(foundation, ledger)
        permit = permit_for(original)
        real = pool.connection
        calls = 0

        @contextmanager
        def lose_ack(*args: Any, **kwargs: Any) -> Iterator[Any]:
            nonlocal calls
            calls += 1
            with real(*args, **kwargs) as connection:
                yield connection
            raise psycopg.OperationalError("synthetic lost command deletion acknowledgment")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pool, "connection", lose_ack)
            with pytest.raises(SubmissionOutcomeUnknownError):
                ledger.redact_execution_command(permit)
        assert calls == 1
        snapshot = ledger.query_outcome("intent-a")
        assert snapshot is not None and snapshot.intent.version == original.version + 1
        before = row(foundation)
        assert before["request_document"] is None
        assert ledger.redact_execution_command(permit).replayed
        assert row(foundation) == before
    finally:
        pool.close()
