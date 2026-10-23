"""Real PG/InnoDB conditional observation, unchanged budgets and no inferred stop."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from speechnexus_contracts.validation import validate_coordination
from tests.fake.execution import fake_ingress
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_recovery import Ledger, pending
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import counters, new_pool
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import await_gateway_lock_wait, request_for
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.application.observe_execution import ExecutionObservationService
from speechnexus_gateway.domain.observation import ExecutionObservation, RecordExecutionObservation
from speechnexus_gateway.domain.recovery import ExecutionSnapshot
from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    StaleSubmissionVersionError,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.interfaces.observation_transactions import ExecutionObservationBoundary

pytestmark = pytest.mark.integration


def observed_request(
    intent: SubmissionIntent, *, unsent: bool = False
) -> RecordExecutionObservation:
    at = datetime.now(UTC)
    return RecordExecutionObservation(
        replace(
            ExecutionObservation.from_intent(intent),
            execution_state=ExecutionState.FAILED if unsent else ExecutionState.EXECUTING,
            cleanup_state=CleanupState.NOT_REQUIRED if unsent else CleanupState.PENDING,
            dispatched_at=None if unsent else at,
            engine_request_id=None if unsent else "engine-request-observed",
            observed_at=at,
            error_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN if unsent else None,
        ),
        intent.version,
    )


@pytest.mark.parametrize("unsent", [False, True])
def test_complete_observation_is_conditional_and_preserves_budget_and_stop_history(
    foundation: Foundation, recovery_ledger: Ledger, unsent: bool
) -> None:
    original = pending(foundation, recovery_ledger)
    before = counters(foundation)
    request = observed_request(original, unsent=unsent)
    written = recovery_ledger.record_execution_observation(request)
    assert written.version == 2 and written.identity == original.identity
    assert written.request_digest == original.request_digest
    assert recovery_ledger.get("intent-a") == written
    assert written.dispatched_at == request.observation.dispatched_at
    assert written.updated_at >= request.observation.observed_at
    assert written.stop_evidence_digest is None
    assert foundation.database.query("SELECT * FROM execution_stop_evidence") == []
    with pytest.raises(StaleSubmissionVersionError):
        recovery_ledger.record_execution_observation(request)
    assert (
        recovery_ledger.record_execution_observation(
            RecordExecutionObservation.from_intent(written, expected_version=2)
        )
        == written
    )
    assert counters(foundation) == before
    boundary = ExecutionObservationBoundary(
        ExecutionObservationService(
            ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
            ledger=recovery_ledger,
        )
    )
    wire_observation = ExecutionSnapshot(written).status()
    watermark = wire_observation["output_watermark"]
    assert isinstance(watermark, dict)
    watermark["generated_pcm_samples"] = 0.0  # JSON Schema integers include whole numeric values.
    result = boundary.record_execution_observation(
        {"observation": wire_observation, "expected_version": "2"}
    )
    assert result["outcome"] == "replayed"
    assert type(watermark["generated_pcm_samples"]) is float  # The wire input was not mutated.
    validate_coordination("TransactionResult", result)


@pytest.mark.parametrize(
    "column,value",
    [("staging_output_object_key", "synthetic-staging"), ("generated_pcm_samples", 12000)],
)
def test_stored_output_prevents_a_false_never_dispatched_cleanup(
    foundation: Foundation, recovery_ledger: Ledger, column: str, value: object
) -> None:
    pending(foundation, recovery_ledger)
    assert column in {"staging_output_object_key", "generated_pcm_samples"}
    foundation.database.query(
        f"UPDATE submit_intents SET {column} = %s WHERE submit_intent_id = 'intent-a'", (value,)
    )
    original = recovery_ledger.get("intent-a")
    assert original is not None
    before = counters(foundation)
    with pytest.raises(ExecutionCleanupPendingError):
        recovery_ledger.record_execution_observation(observed_request(original, unsent=True))
    assert recovery_ledger.get("intent-a") == original
    assert counters(foundation) == before


def test_one_of_two_concurrent_observers_wins_the_version(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    request = observed_request(pending(foundation, recovery_ledger))

    def observe(_: int) -> SubmissionIntent | None:
        try:
            return recovery_ledger.record_execution_observation(request)
        except StaleSubmissionVersionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        result = list(workers.map(observe, range(2)))
    assert sum(row is not None for row in result) == 1
    stored = recovery_ledger.get("intent-a")
    assert stored is not None and stored.version == 2


def test_failed_observation_write_rolls_back_without_replacing_the_intent(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    before = counters(foundation)
    foundation.database.query(
        "ALTER TABLE submit_intents ADD CONSTRAINT injected_observation_failure "
        "CHECK (execution_state <> 'executing')"
    )
    with pytest.raises(SubmissionStoreError):
        recovery_ledger.record_execution_observation(observed_request(original))
    assert recovery_ledger.get("intent-a") == original
    assert counters(foundation) == before


def test_changed_generation_or_watermark_does_not_replace_the_original(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    request = observed_request(original)
    for changed in (
        replace(request.observation, identity=replace(original.identity, instance_generation=2)),
        replace(request.observation, generated_pcm_samples=1),
    ):
        with pytest.raises(InvalidExecutionRequestError):
            recovery_ledger.record_execution_observation(replace(request, observation=changed))
    assert recovery_ledger.get("intent-a") == original


def test_first_write_cannot_import_a_preclaimed_no_cleanup_result(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    preclaimed = replace(
        original,
        execution_state=ExecutionState.FAILED,
        cleanup_state=CleanupState.NOT_REQUIRED,
        error_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
    )
    with pytest.raises(InvalidExecutionRequestError):
        recovery_ledger.add(
            preclaimed, request_document=request_for(original.identity).request_document
        )
    assert recovery_ledger.get("intent-a") == original


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_commit_ack_loss_queries_the_original_observation_without_an_automatic_second_write(
    foundation: Foundation,
) -> None:
    pool = new_pool(foundation)
    try:
        ledger = PostgresSubmissionLedger(pool)
        request = observed_request(pending(foundation, ledger))
        real = pool.connection
        calls = 0

        @contextmanager
        def lose_ack(*args: Any, **kwargs: Any) -> Iterator[Any]:
            nonlocal calls
            calls += 1
            with real(*args, **kwargs) as connection:
                yield connection
            raise psycopg.OperationalError("synthetic lost observation acknowledgment")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pool, "connection", lose_ack)
            with pytest.raises(SubmissionOutcomeUnknownError):
                ledger.record_execution_observation(request)
        assert calls == 1
        snapshot = ledger.query_outcome("intent-a")
        assert snapshot is not None and snapshot.intent.version == 2
        with pytest.raises(StaleSubmissionVersionError):
            ledger.record_execution_observation(request)
    finally:
        pool.close()


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_observation_clock_is_sampled_after_waiting_for_the_pool_lock(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    request = observed_request(pending(foundation, recovery_ledger))
    with foundation.database.blocker() as blocker:
        blocker.execute(
            "SELECT pool_id FROM resource_pools WHERE pool_id = 'tts-realtime' FOR UPDATE"
        )
        with ThreadPoolExecutor(max_workers=1) as workers:
            writing = workers.submit(recovery_ledger.record_execution_observation, request)
            try:
                waits = await_gateway_lock_wait(foundation)
                assert waits and "resource_pools" in waits[0]["query"]
                unchanged = recovery_ledger.get("intent-a")
                assert unchanged is not None and unchanged.version == 1
                released_at = datetime.now(UTC)
            finally:
                blocker.rollback()
            written = writing.result(timeout=5)
            assert written.updated_at >= released_at
