"""Shared PG/InnoDB recovery transactions, original reads and unchanged A budgets."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from speechnexus_contracts.execution import parse
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_stop_evidence import counters, evidence_for, new_pool
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import (
    identity_for,
    request_for,
    seed_second_attempt,
)
from tests.integration.test_production_jobs import database as database
from tests.support.contracts import validator
from tests.support.gateway_database import MySQLSubmissionLedger

from speechnexus_gateway.domain.recovery import (
    ExecutionObservationConflictError,
    MarkExecutionUnknown,
)
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    RecordStop,
    StopEvidenceKind,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

pytestmark = pytest.mark.integration
type Ledger = PostgresSubmissionLedger | MySQLSubmissionLedger


@pytest.fixture
def recovery_ledger(stop_ledger: object) -> Ledger:
    assert isinstance(stop_ledger, PostgresSubmissionLedger | MySQLSubmissionLedger)
    return stop_ledger


def pending(foundation: Foundation, ledger: Ledger) -> SubmissionIntent:
    seed_second_attempt(foundation)
    request = request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a"))
    now = datetime.now(UTC)
    intent = SubmissionIntent(
        identity=request.identity,
        recorded_at=now,
        updated_at=now,
        request_digest=request.request_digest,
        output_stream_id=request.output_stream_id,
    )
    ledger.add(intent, request_document=request.request_document)
    return intent


def unknown_request(foundation: Foundation) -> MarkExecutionUnknown:
    return MarkExecutionUnknown(
        "intent-a",
        ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
        evidence_for(foundation).observed_at_us,
    )


@pytest.mark.parametrize(
    "kind",
    [
        StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
        StopEvidenceKind.OPERATOR_VERIFIED_INSTANCE_TERMINATED,
    ],
)
def test_orphan_intent_is_quarantined_and_closed_only_by_verified_instance_stop(
    foundation: Foundation, recovery_ledger: Ledger, kind: StopEvidenceKind
) -> None:
    original = pending(foundation, recovery_ledger)
    # Synthetic persisted output from before the crash, not a new metering implementation.
    foundation.database.query(
        "UPDATE submit_intents SET generated_pcm_samples = 12000, "
        "staging_output_object_key = 'synthetic-private-staging' "
        "WHERE submit_intent_id = 'intent-a'"
    )
    before = counters(foundation)
    request = unknown_request(foundation)
    unknown = recovery_ledger.mark_execution_unknown(request)
    assert unknown.dispatched_at is None and unknown.engine_request_id is None
    assert (
        unknown.identity == original.identity and unknown.request_digest == original.request_digest
    )
    assert recovery_ledger.mark_execution_unknown(request) == unknown
    assert counters(foundation) == before
    snapshot = recovery_ledger.query_outcome("intent-a")
    assert snapshot is not None
    parse("ExecutionStatusResponse", snapshot.status())
    validator("internal/coordination-transactions.schema.json", "ExecutionSnapshot").validate(
        snapshot.response()
    )
    assert snapshot.status()["output_watermark"] == {
        "output_stream_id": "tts-main",
        "generated_pcm_samples": 12000,
        "sample_rate_hz": 24000,
        "hard_output_ceiling_samples": 48000,
    }
    assert "staging_artifact_ref" not in snapshot.status()
    assert "synthetic-private-staging" not in str(snapshot.response())
    for ack_kind in (StopEvidenceKind.ENGINE_ABORT_ACK, StopEvidenceKind.ENGINE_TERMINAL_RESPONSE):
        with pytest.raises(ExecutionCleanupPendingError):
            recovery_ledger.record_stop(
                RecordStop(evidence_for(foundation, evidence_kind=ack_kind), ExecutionState.ABORTED)
            )
    evidence = evidence_for(
        foundation, evidence_kind=kind, reason="synthetic verified old generation termination"
    )
    closed = recovery_ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED))
    assert closed.dispatched_at is None and closed.engine_request_id is None
    assert closed.generated_pcm_samples == 12000
    assert recovery_ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED)) == closed
    assert recovery_ledger.get("intent-a") == closed
    closed_snapshot = recovery_ledger.query_outcome("intent-a")
    assert closed_snapshot is not None
    parse("ExecutionStatusResponse", closed_snapshot.status())
    assert counters(foundation) == before
    with pytest.raises(ExecutionCleanupPendingError):
        recovery_ledger.mark_execution_unknown(request)


def test_unknown_conflicts_and_future_observations_do_not_rewrite_original(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    pending(foundation, recovery_ledger)
    request = unknown_request(foundation)
    with pytest.raises(InvalidExecutionRequestError):
        recovery_ledger.mark_execution_unknown(
            replace(request, observed_at_us=request.observed_at_us + 60_000_000)
        )
    original = recovery_ledger.mark_execution_unknown(request)
    with pytest.raises(ExecutionObservationConflictError):
        recovery_ledger.mark_execution_unknown(
            replace(request, reason_code=ExecutionErrorCode.CANCELLATION_UNCONFIRMED)
        )
    assert recovery_ledger.get("intent-a") == original
    assert recovery_ledger.query_outcome("missing-intent") is None


def test_generated_output_cannot_be_read_as_a_proven_unsent_failure(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    pending(foundation, recovery_ledger)
    before = counters(foundation)
    foundation.database.query(
        "UPDATE submit_intents SET execution_state = 'failed', cleanup_state = 'not_required', "
        "error_code = 'engine_transport_unknown', generated_pcm_samples = 12000 "
        "WHERE submit_intent_id = 'intent-a'"
    )
    with pytest.raises(SubmissionStoreError) as error:
        recovery_ledger.query_outcome("intent-a")
    assert error.value.recovery == "reconcile"
    assert counters(foundation) == before


def test_concurrent_unknown_requests_store_one_transition(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    pending(foundation, recovery_ledger)
    request = unknown_request(foundation)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(lambda _: recovery_ledger.mark_execution_unknown(request), range(2))
        )
    assert results[0] == results[1]
    assert results[0].version == 2


def test_failed_unknown_write_rolls_back_without_touching_budget(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    before = counters(foundation)
    foundation.database.query(
        "ALTER TABLE submit_intents ADD CONSTRAINT injected_unknown_failure "
        "CHECK (execution_state <> 'unknown_quarantined')"
    )
    with pytest.raises(SubmissionStoreError):
        recovery_ledger.mark_execution_unknown(unknown_request(foundation))
    assert recovery_ledger.get("intent-a") == original
    assert counters(foundation) == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_unknown_commit_ack_loss_recovers_original_without_dispatch_or_second_version(
    foundation: Foundation,
) -> None:
    pool = new_pool(foundation)
    try:
        ledger = PostgresSubmissionLedger(pool)
        pending(foundation, ledger)
        request = unknown_request(foundation)
        real = pool.connection
        calls = 0

        @contextmanager
        def lose_ack(*args: Any, **kwargs: Any) -> Iterator[Any]:
            nonlocal calls
            calls += 1
            with real(*args, **kwargs) as connection:
                yield connection
            raise psycopg.OperationalError("synthetic lost commit acknowledgment")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pool, "connection", lose_ack)
            with pytest.raises(SubmissionOutcomeUnknownError):
                ledger.mark_execution_unknown(request)
        assert calls == 1
        snapshot = ledger.query_outcome("intent-a")
        assert snapshot is not None and snapshot.intent.version == 2
        assert snapshot.intent.dispatched_at is None
        assert ledger.mark_execution_unknown(request) == snapshot.intent
    finally:
        pool.close()
