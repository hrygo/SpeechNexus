"""Atomic stop facts, concurrency and unchanged budgets on PG and InnoDB.

PostgreSQL uses the production adapter and non-owner gateway pool. InnoDB runs the same
ordinary SQL and domain transitions through test-only DB-API plumbing.
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from tests.fake.execution import ScriptedEngine
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_submit_ledger import (
    identity_for,
    request_for,
    seed_second_attempt,
)
from tests.integration.test_production_jobs import database as database
from tests.support.gateway_database import MySQLSubmissionLedger

from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.ports import DispatchOutcome, SubmissionLedger
from speechnexus_gateway.domain.submission import (
    CancellationUnconfirmedError,
    CleanupState,
    DispatchResult,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    StopEvidenceConflictError,
    StopEvidenceKind,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    VerifiedStopEvidence,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool, gateway_pool
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "e" * 64


def new_pool(foundation: Foundation) -> GatewayPool:
    pool = gateway_pool(
        make_conninfo(
            foundation.database.url,
            user=foundation.gateway_role,
            password="synthetic_foundation_test",
        ),
        schema=foundation.database.schema,
    )
    pool.open()
    return pool


@pytest.fixture
def stop_ledger(foundation: Foundation) -> Iterator[SubmissionLedger]:
    if foundation.database.dialect == "mysql":
        yield MySQLSubmissionLedger(foundation)
        return
    pool = new_pool(foundation)
    try:
        yield PostgresSubmissionLedger(pool)
    finally:
        pool.close()


def start_execution(
    foundation: Foundation,
    ledger: SubmissionLedger,
    *,
    unknown: bool = False,
) -> tuple[SubmitExecutionService, ScriptedEngine]:
    seed_second_attempt(foundation)
    engine = ScriptedEngine()
    if unknown:
        engine.script_outcome(DispatchOutcome(DispatchResult.RESPONSE_LOST, datetime.now(UTC)))
    service = SubmitExecutionService(ledger=ledger, dispatch=engine, observation=engine)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))
    return service, engine


def evidence_for(foundation: Foundation, **changes: Any) -> VerifiedStopEvidence:
    clock = (
        "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        if foundation.database.dialect == "postgresql"
        else "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000 AS SIGNED) AS now_us"
    )
    return replace(
        VerifiedStopEvidence(
            evidence_id="stop-evidence-a",
            submit_intent_id="intent-a",
            evidence_kind=StopEvidenceKind.ENGINE_ABORT_ACK,
            evidence_digest=DIGEST,
            observed_at_us=foundation.database.query(clock)[0]["now_us"],
            execution_stopped=True,
            resources_released=True,
            reason=None,
        ),
        **changes,
    )


def counters(foundation: Foundation) -> list[dict[str, Any]]:
    return foundation.database.query(
        "SELECT h.held_unified_physical_memory_bytes, h.quarantined_unified_physical_memory_bytes, "
        "i.held_slots, i.quarantined_slots, q.held_attempts, q.quarantined_attempts, "
        "l.lease_state, l.cleanup_state, l.version FROM host_nodes h "
        "JOIN engine_instances i ON i.host_node_id = h.host_node_id "
        "JOIN admission_leases l ON l.instance_id = i.instance_id "
        "AND l.instance_generation = i.instance_generation "
        "JOIN admission_attempts a ON a.operation_id = l.operation_id "
        "AND a.attempt_id = l.attempt_id "
        "JOIN workspace_quotas q ON q.workspace_id = a.workspace_id ORDER BY l.lease_id"
    )


@pytest.mark.parametrize(
    "kind",
    [
        StopEvidenceKind.ENGINE_ABORT_ACK,
        StopEvidenceKind.ENGINE_TERMINAL_RESPONSE,
        StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
        StopEvidenceKind.OPERATOR_VERIFIED_INSTANCE_TERMINATED,
    ],
)
def test_verified_stop_is_durable_idempotent_and_does_not_release_budgets(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    kind: StopEvidenceKind,
) -> None:
    service, engine = start_execution(foundation, stop_ledger)
    before = counters(foundation)
    evidence = evidence_for(foundation, evidence_kind=kind, reason="synthetic verification")
    first = service.record_stop(evidence)
    second = service.record_stop(evidence)
    assert first == second == stop_ledger.get("intent-a")
    assert first.execution_state is ExecutionState.ABORTED
    assert first.cleanup_state is CleanupState.COMPLETE
    assert first.version == 3
    assert first.stop_evidence_digest == evidence.evidence_digest
    assert first.terminated_at is not None
    assert counters(foundation) == before
    assert engine.dispatch_count == 1
    row = foundation.database.query(
        "SELECT evidence_id, observed_at_us, reason FROM execution_stop_evidence"
    )
    assert row == [
        {
            "evidence_id": evidence.evidence_id,
            "observed_at_us": evidence.observed_at_us,
            "reason": evidence.reason,
        }
    ]


@pytest.mark.parametrize("state", [ExecutionState.SUCCEEDED, ExecutionState.FAILED])
def test_terminal_engine_response_requires_and_saves_release_evidence(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    state: ExecutionState,
) -> None:
    service, _ = start_execution(foundation, stop_ledger)
    evidence = evidence_for(foundation, evidence_kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE)
    error = ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN if state is ExecutionState.FAILED else None
    closed = service.record_stop(evidence, terminal_state=state, error_code=error)
    assert stop_ledger.get("intent-a") == closed
    assert service.record_stop(evidence, terminal_state=state, error_code=error) == closed
    assert closed.error_code == error
    assert closed.execution_state is state
    assert (
        foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")[0]["n"] == 1
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_id": "stop-evidence-other"},
        {"evidence_digest": "sha256:" + "f" * 64},
        {"reason": "another reason"},
        {"observed_at_us": 1},
        {"evidence_kind": StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER},
    ],
)
def test_stop_replay_compares_the_complete_fact(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    changes: dict[str, Any],
) -> None:
    service, _ = start_execution(foundation, stop_ledger)
    evidence = evidence_for(foundation)
    original = service.record_stop(evidence)
    with pytest.raises(StopEvidenceConflictError):
        service.record_stop(replace(evidence, **changes))
    assert stop_ledger.get("intent-a") == original
    assert (
        foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")[0]["n"] == 1
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_stopped": False},
        {"resources_released": False},
        {"evidence_kind": StopEvidenceKind.UNCONFIRMED_TIMEOUT},
        {"evidence_kind": StopEvidenceKind.NONE},
    ],
)
def test_incomplete_evidence_cannot_close_or_release_anything(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    changes: dict[str, Any],
) -> None:
    service, _ = start_execution(foundation, stop_ledger)
    original = stop_ledger.get("intent-a")
    before = counters(foundation)
    with pytest.raises(CancellationUnconfirmedError):
        service.record_stop(evidence_for(foundation, **changes))
    assert stop_ledger.get("intent-a") == original
    assert counters(foundation) == before
    assert (
        foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")[0]["n"] == 0
    )


def test_future_observation_is_not_a_stop_fact(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
) -> None:
    service, _ = start_execution(foundation, stop_ledger)
    evidence = evidence_for(foundation)
    with pytest.raises(ExecutionCleanupPendingError):
        service.record_stop(replace(evidence, observed_at_us=evidence.observed_at_us + 60_000_000))
    stored = stop_ledger.get("intent-a")
    assert stored is not None and stored.execution_state is ExecutionState.EXECUTING
    assert (
        foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")[0]["n"] == 0
    )


def test_failure_after_evidence_insert_rolls_back_the_entire_stop(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
) -> None:
    service, _ = start_execution(foundation, stop_ledger)
    before = stop_ledger.get("intent-a")
    foundation.database.query(
        "ALTER TABLE submit_intents ADD CONSTRAINT injected_stop_failure "
        "CHECK (execution_state <> 'aborted')"
    )
    with pytest.raises(SubmissionStoreError):
        service.record_stop(evidence_for(foundation))
    assert stop_ledger.get("intent-a") == before
    assert (
        foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")[0]["n"] == 0
    )


def test_competing_identical_stops_commit_one_fact(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
) -> None:
    service, _ = start_execution(foundation, stop_ledger)
    evidence = evidence_for(foundation)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: service.record_stop(evidence), range(2)))
    assert results[0] == results[1]
    assert results[0].version == 3
    assert (
        foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")[0]["n"] == 1
    )


def test_competing_conflicting_stops_preserve_the_first_complete_fact(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
) -> None:
    service, _ = start_execution(foundation, stop_ledger)
    first = evidence_for(foundation)
    second = replace(first, evidence_digest="sha256:" + "f" * 64)

    def attempt(evidence: VerifiedStopEvidence) -> SubmissionIntent | None:
        try:
            return service.record_stop(evidence)
        except StopEvidenceConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, [first, second]))
    committed = [result for result in results if result is not None]
    assert len(committed) == 1
    assert committed[0] == stop_ledger.get("intent-a")
    row = foundation.database.query("SELECT evidence_digest FROM execution_stop_evidence")[0]
    assert row["evidence_digest"] == committed[0].stop_evidence_digest


def test_closed_execution_cannot_be_resurrected_by_a_late_save(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
) -> None:
    service, _ = start_execution(foundation, stop_ledger)
    closed = service.record_stop(evidence_for(foundation))
    changed = replace(closed)
    changed.mark_dispatched("engine-request-ghost", at=datetime.now(UTC))
    with pytest.raises(ExecutionCleanupPendingError):
        stop_ledger.save(changed, expected_version=closed.version)
    assert stop_ledger.get("intent-a") == closed


def test_execution_error_and_terminal_state_are_part_of_replay_identity(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
) -> None:
    service, _ = start_execution(foundation, stop_ledger)
    evidence = evidence_for(foundation, evidence_kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE)
    closed = service.record_stop(
        evidence,
        terminal_state=ExecutionState.FAILED,
        error_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
    )
    with pytest.raises(StopEvidenceConflictError):
        service.record_stop(
            evidence,
            terminal_state=ExecutionState.FAILED,
            error_code=ExecutionErrorCode.CANCELLATION_UNCONFIRMED,
        )
    with pytest.raises(StopEvidenceConflictError):
        service.record_stop(evidence, terminal_state=ExecutionState.SUCCEEDED)
    assert stop_ledger.get("intent-a") == closed


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_commit_ack_loss_recovers_the_original_stop_without_a_second_write(
    foundation: Foundation,
) -> None:
    pool = new_pool(foundation)
    try:
        real = pool.connection
        calls = 0

        @contextmanager
        def lose_ack(*args: Any, **kwargs: Any) -> Iterator[Any]:
            nonlocal calls
            calls += 1
            with real(*args, **kwargs) as connection:
                yield connection
            raise psycopg.OperationalError("synthetic lost commit acknowledgment")

        ledger = PostgresSubmissionLedger(pool)
        service, engine = start_execution(foundation, ledger)
        evidence = evidence_for(foundation)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pool, "connection", lose_ack)
            with pytest.raises(SubmissionOutcomeUnknownError):
                service.record_stop(evidence)
        assert calls == 1
        recovered = ledger.get("intent-a")
        assert recovered is not None and recovered.execution_state is ExecutionState.ABORTED
        replay = service.record_stop(evidence)
        assert recovered == replay
        assert engine.dispatch_count == 1
        assert (
            foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")[0]["n"]
            == 1
        )
    finally:
        pool.close()


def test_an_evidence_id_cannot_be_reused_for_another_execution(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
) -> None:
    service, engine = start_execution(foundation, stop_ledger)
    service.submit(
        request_for(
            identity_for(
                "lease-alt-stage-alt",
                submit_intent_id="intent-b",
                stage_id="stage-alt",
                fencing_token=2,
            )
        )
    )
    evidence = evidence_for(foundation)
    first = service.record_stop(evidence)
    with pytest.raises(StopEvidenceConflictError):
        service.record_stop(replace(evidence, submit_intent_id="intent-b"))
    assert stop_ledger.get("intent-a") == first
    active = stop_ledger.get("intent-b")
    assert active is not None and active.execution_state is ExecutionState.EXECUTING
    assert engine.dispatch_count == 2
    assert (
        foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")[0]["n"] == 1
    )


@pytest.mark.parametrize("state", ["succeeded", "failed", "aborted"])
def test_a_terminal_row_without_evidence_requires_reconciliation(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    state: str,
) -> None:
    start_execution(foundation, stop_ledger)
    foundation.database.query(
        "UPDATE submit_intents SET execution_state = %s, cleanup_state = 'complete', "
        "terminated_at_us = 1 WHERE submit_intent_id = 'intent-a'",
        (state,),
    )
    with pytest.raises(SubmissionStoreError):
        stop_ledger.get("intent-a")


def test_unknown_execution_keeps_budget_until_the_verified_abort_is_recorded(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
) -> None:
    service, engine = start_execution(foundation, stop_ledger, unknown=True)
    before = counters(foundation)
    active = stop_ledger.get("intent-a")
    assert active is not None and active.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    with pytest.raises(CancellationUnconfirmedError):
        service.record_stop(evidence_for(foundation, resources_released=False))
    assert stop_ledger.get("intent-a") == active
    stopped = service.record_stop(evidence_for(foundation))
    assert stopped.execution_state is ExecutionState.ABORTED
    assert stopped.cleanup_state is CleanupState.COMPLETE
    assert counters(foundation) == before
    assert engine.dispatch_count == 1


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_stop_takes_the_pool_lock_before_modifying_execution_facts(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
) -> None:
    from tests.integration.test_gateway_submit_ledger import await_gateway_lock_wait

    service, _ = start_execution(foundation, stop_ledger)
    evidence = evidence_for(foundation)
    with foundation.database.blocker() as blocker:
        blocker.execute(
            "SELECT pool_id FROM resource_pools WHERE pool_id = 'tts-realtime' FOR UPDATE"
        )
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(service.record_stop, evidence)
            try:
                waits = await_gateway_lock_wait(foundation)
                assert waits and "resource_pools" in waits[0]["query"]
                assert (
                    foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")[
                        0
                    ]["n"]
                    == 0
                )
            finally:
                blocker.rollback()
            assert pending.result(timeout=5).execution_state is ExecutionState.ABORTED
