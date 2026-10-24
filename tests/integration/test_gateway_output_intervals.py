"""PG/InnoDB append-only output facts; cross-pool stages share the same stream mutex."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg
import pymysql
import pytest
from speechnexus_contracts.validation import validate_coordination
from tests.fake.execution import fake_ingress
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_command_redaction import release_fixture
from tests.integration.test_gateway_recovery import Ledger, pending
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import counters, evidence_for, new_pool
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import (
    identity_for,
    request_for,
    seed_second_attempt,
)
from tests.integration.test_production_jobs import database as database
from tests.support.gateway_locks import wait_for_gateway_locks

from speechnexus_gateway.application.output_intervals import OutputIntervalService
from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    RedactExecutionCommand,
)
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.output_intervals import (
    AppendInterval,
    MeterKind,
    OutputIntervalConflictError,
    OutputIntervalRecord,
    OutputOverlapError,
)
from speechnexus_gateway.domain.recovery import MarkExecutionUnknown
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
from speechnexus_gateway.interfaces.output_interval_transactions import OutputIntervalBoundary

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "d" * 64


def interval(start: int = 0, end: int = 100, **changes: Any) -> AppendInterval:
    return replace(
        AppendInterval(
            "interval-a", "intent-a", "tts-main", 24000, MeterKind.GENERATED, start, end
        ),
        **changes,
    )


def boundary_for(ledger: Ledger) -> OutputIntervalBoundary:
    return OutputIntervalBoundary(
        OutputIntervalService(
            ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"), ledger=ledger
        )
    )


def interval_rows(foundation: Foundation) -> list[dict[str, Any]]:
    return foundation.database.query("SELECT * FROM output_sample_intervals ORDER BY interval_id")


def cross_pool_pair(
    foundation: Foundation, ledger: Ledger
) -> tuple[SubmissionIntent, SubmissionIntent]:
    first = pending(foundation, ledger)
    foundation.database.query(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES ('tts-batch', 'batch', 'active')"
    )
    foundation.database.query(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id, "
        "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
        "loading_policy, config_digest, instance_state, max_slots) "
        "SELECT 'instance-alt', 1, host_node_id, 'tts-batch', gateway_id, binding_revision_id, "
        "manifest_id, approved_template_id, management_mode, loading_policy, config_digest, "
        "instance_state, max_slots FROM engine_instances WHERE instance_id = 'instance-test'"
    )
    foundation.database.query(
        "INSERT INTO runtime_instance_heads "
        "(instance_id, current_generation, generation_high_water) "
        "VALUES ('instance-alt', 1, 1)"
    )
    foundation.database.query(
        "UPDATE admission_leases SET pool_id = 'tts-batch', instance_id = 'instance-alt' "
        "WHERE lease_id = 'lease-alt-stage-alt'"
    )
    identity = replace(
        identity_for(
            "lease-alt-stage-alt",
            submit_intent_id="intent-b",
            stage_id="stage-alt",
            fencing_token=2,
        ),
        pool_id="tts-batch",
        instance_id="instance-alt",
    )
    request = request_for(identity)
    now = datetime.now(UTC)
    second = SubmissionIntent(
        identity, now, now, request.request_digest, output_stream_id="tts-main"
    )
    ledger.add(second, request_document=request.request_document)
    return first, second


def test_intervals_are_canonical_durable_facts_and_diagnostics_leave_generated_usage_alone(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    budgets = counters(foundation)
    stops = foundation.database.query("SELECT * FROM execution_stop_evidence")
    heads = foundation.database.query("SELECT * FROM output_stream_heads ORDER BY output_stream_id")
    boundary = boundary_for(recovery_ledger)
    for kind in MeterKind:
        value = interval(interval_id=f"interval-{kind.value}", meter_kind=kind)
        result = boundary.append_interval(value.document())
        replay = boundary.append_interval(value.document())
        assert result["outcome"] == "committed" and replay["outcome"] == "replayed"
        assert "version" not in result and replay["value"] == result["value"]
        recovered = boundary.query_outcome(
            {
                "target": {"kind": "interval", "interval_id": value.interval_id},
                "request_digest": result["value"]["request_digest"],
            }
        )
        assert recovered["value"] == result["value"]
        validate_coordination("TransactionResult", recovered)
    current = recovery_ledger.get("intent-a")
    assert current is not None and current.version == original.version + 1
    assert current.generated_pcm_samples == 100 and current.identity == original.identity
    assert current.dispatched_at is None and current.engine_request_id is None
    assert counters(foundation) == budgets
    assert foundation.database.query("SELECT * FROM execution_stop_evidence") == stops
    assert (
        foundation.database.query("SELECT * FROM output_stream_heads ORDER BY output_stream_id")
        == heads
    )
    assert len(interval_rows(foundation)) == 3


def test_adjacent_gaps_and_out_of_order_intervals_never_turn_a_watermark_into_usage(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    for start, end, name in [
        (100, 200, "interval-a"),
        (0, 100, "interval-b"),
        (300, 400, "interval-c"),
    ]:
        recovery_ledger.append_interval(
            OutputIntervalRecord(interval(start, end, interval_id=name), original.identity)
        )
    current = recovery_ledger.get("intent-a")
    assert current is not None and current.generated_pcm_samples == 400
    rows = interval_rows(foundation)
    assert sum(row["sample_end"] - row["sample_start"] for row in rows) == 300
    assert current.version == original.version + 3


@pytest.mark.parametrize("overlap", [False, True])
def test_two_pools_really_wait_for_one_shared_stream_before_deciding_overlap(
    foundation: Foundation, recovery_ledger: Ledger, overlap: bool
) -> None:
    first, second = cross_pool_pair(foundation, recovery_ledger)
    records = (
        OutputIntervalRecord(interval(), first.identity),
        OutputIntervalRecord(
            interval(
                50 if overlap else 100,
                150 if overlap else 200,
                interval_id="interval-b",
                submit_intent_id="intent-b",
            ),
            second.identity,
        ),
    )

    def append(record: OutputIntervalRecord) -> str:
        try:
            return "replayed" if recovery_ledger.append_interval(record).replayed else "committed"
        except OutputOverlapError:
            return "overlap"

    before = counters(foundation)
    with foundation.database.blocker() as blocker:
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT sample_rate_hz FROM output_stream_heads "
                "WHERE operation_id = 'operation-test' "
                "AND attempt_id = 'attempt-alt' AND output_stream_id = 'tts-main' FOR UPDATE"
            )
        with ThreadPoolExecutor(max_workers=2) as workers:
            waiting = [workers.submit(append, record) for record in records]
            try:
                wait_for_gateway_locks(foundation, "output_stream_heads", minimum=2)
                assert interval_rows(foundation) == []
                assert recovery_ledger.query_output_interval("interval-a") is None
                blocker.rollback()
            finally:
                blocker.rollback()
            outcomes = [future.result(timeout=5) for future in waiting]
    assert sorted(outcomes) == sorted(["committed", "overlap" if overlap else "committed"])
    assert len(interval_rows(foundation)) == (1 if overlap else 2)
    assert counters(foundation) == before


def test_two_identical_writers_commit_once_and_replay_one_original_fact(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    record = OutputIntervalRecord(interval(), original.identity)
    with foundation.database.blocker() as blocker:
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT pool_id FROM resource_pools WHERE pool_id = 'tts-realtime' FOR UPDATE"
            )
        with ThreadPoolExecutor(max_workers=2) as workers:
            waiting = [workers.submit(recovery_ledger.append_interval, record) for _ in range(2)]
            try:
                wait_for_gateway_locks(foundation, "resource_pools", minimum=2)
                blocker.rollback()
            finally:
                blocker.rollback()
            results = [future.result(timeout=5) for future in waiting]
    assert sorted(result.replayed for result in results) == [False, True]
    current = recovery_ledger.get("intent-a")
    assert current is not None and current.version == original.version + 1
    assert len(interval_rows(foundation)) == 1


def test_interval_id_cannot_be_reused_with_another_submit_or_range(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    first, second = cross_pool_pair(foundation, recovery_ledger)
    recovery_ledger.append_interval(OutputIntervalRecord(interval(), first.identity))
    before = interval_rows(foundation)
    for record in (
        OutputIntervalRecord(interval(end=101), first.identity),
        OutputIntervalRecord(interval(100, 200, submit_intent_id="intent-b"), second.identity),
    ):
        with pytest.raises(OutputIntervalConflictError):
            recovery_ledger.append_interval(record)
    assert interval_rows(foundation) == before


@pytest.mark.parametrize("kind", list(MeterKind))
def test_a_frozen_ceiling_is_enforced_before_any_interval_or_watermark_write(
    foundation: Foundation, recovery_ledger: Ledger, kind: MeterKind
) -> None:
    original = pending(foundation, recovery_ledger)
    rejected = boundary_for(recovery_ledger).append_interval(
        interval(48000, 48001, meter_kind=kind).document()
    )
    assert rejected["error_code"] == "output_limit_exceeded" and rejected["recovery"] == "reconcile"
    assert recovery_ledger.get("intent-a") == original and interval_rows(foundation) == []


def test_a_projection_update_failure_rolls_back_the_prior_interval_insert(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    foundation.database.query(
        "ALTER TABLE submit_intents ADD CONSTRAINT injected_interval_failure "
        "CHECK (generated_pcm_samples = 0)"
    )
    with pytest.raises(SubmissionStoreError):
        recovery_ledger.append_interval(OutputIntervalRecord(interval(), original.identity))
    assert recovery_ledger.get("intent-a") == original and interval_rows(foundation) == []


def test_a_non_pcm_execution_and_a_forged_generation_cannot_receive_a_pcm_interval(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    seed_second_attempt(foundation)
    request = request_for(
        identity_for(
            "lease-alt-stage-main",
            submit_intent_id="intent-a",
            sample_rate_hz=None,
            max_output_audio_ms=None,
        ),
        stream=None,
    )
    now = datetime.now(UTC)
    original = SubmissionIntent(request.identity, now, now, request.request_digest)
    recovery_ledger.add(original, request_document=request.request_document)
    rejected = boundary_for(recovery_ledger).append_interval(interval().document())
    assert rejected["error_code"] == "invalid_execution_request"
    with pytest.raises(InvalidExecutionRequestError):
        recovery_ledger.append_interval(
            OutputIntervalRecord(interval(), replace(original.identity, instance_generation=2))
        )
    assert interval_rows(foundation) == [] and recovery_ledger.get("intent-a") == original


@pytest.mark.parametrize(
    "changes",
    [
        {"instance_generation": 2},
        {"max_output_audio_ms": 4000},
        {"voice_revision_id": "voice-other"},
    ],
)
def test_metering_cannot_change_a_frozen_execution_or_output_ceiling(
    foundation: Foundation, recovery_ledger: Ledger, changes: dict[str, Any]
) -> None:
    original = pending(foundation, recovery_ledger)
    with pytest.raises(InvalidExecutionRequestError):
        recovery_ledger.append_interval(
            OutputIntervalRecord(interval(), replace(original.identity, **changes))
        )
    assert interval_rows(foundation) == [] and recovery_ledger.get("intent-a") == original


@pytest.mark.parametrize("kind", list(MeterKind))
def test_a_never_dispatched_failure_cannot_be_given_any_output_interval(
    foundation: Foundation, recovery_ledger: Ledger, kind: MeterKind
) -> None:
    original = pending(foundation, recovery_ledger)
    original.mark_not_dispatched_failure(
        ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, at=original.updated_at
    )
    closed = recovery_ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(original, expected_version=1)
    )
    with pytest.raises(ExecutionCleanupPendingError):
        recovery_ledger.append_interval(
            OutputIntervalRecord(interval(meter_kind=kind), original.identity)
        )
    assert interval_rows(foundation) == [] and recovery_ledger.get("intent-a") == closed


@pytest.mark.parametrize("kind", [MeterKind.DELIVERED, MeterKind.PLAYED])
def test_diagnostic_intervals_block_a_false_unsent_failure_without_charging_generation(
    foundation: Foundation, recovery_ledger: Ledger, kind: MeterKind
) -> None:
    original = pending(foundation, recovery_ledger)
    budgets = counters(foundation)
    recovery_ledger.append_interval(
        OutputIntervalRecord(interval(meter_kind=kind), original.identity)
    )
    current = recovery_ledger.get("intent-a")
    assert (
        current is not None and current.generated_pcm_samples == 0 and current.has_output_intervals
    )
    assert current.version == original.version
    original.mark_not_dispatched_failure(
        ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, at=original.updated_at
    )
    with pytest.raises(ExecutionCleanupPendingError):
        recovery_ledger.record_execution_observation(
            RecordExecutionObservation.from_intent(original, expected_version=1)
        )
    assert recovery_ledger.get("intent-a") == current and counters(foundation) == budgets


def test_stop_and_body_redaction_preserve_the_original_interval_fact(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    saved = recovery_ledger.append_interval(OutputIntervalRecord(interval(), original.identity))
    evidence = evidence_for(
        foundation, evidence_kind=StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER
    )
    recovery_ledger.mark_execution_unknown(
        MarkExecutionUnknown(
            "intent-a", ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, evidence.observed_at_us
        )
    )
    stopped = recovery_ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED))
    release_fixture(foundation)
    before = interval_rows(foundation)
    recovery_ledger.redact_execution_command(
        AuthorizedCommandRedaction(
            RedactExecutionCommand("intent-a", stopped.version, "deletion-test", DIGEST),
            stopped.identity,
        )
    )
    assert recovery_ledger.query_output_interval("interval-a") == saved.record
    assert interval_rows(foundation) == before
    assert (
        foundation.database.query(
            "SELECT request_document FROM submit_intents WHERE submit_intent_id = 'intent-a'"
        )[0]["request_document"]
        is None
    )


def test_runtime_roles_cannot_rewrite_or_delete_an_interval(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    recovery_ledger.append_interval(OutputIntervalRecord(interval(), original.identity))
    before = interval_rows(foundation)
    for owner in ("speech", "gateway"):
        for statement in (
            "UPDATE output_sample_intervals SET sample_end = 101 WHERE interval_id = 'interval-a'",
            "DELETE FROM output_sample_intervals WHERE interval_id = 'interval-a'",
        ):
            with (
                foundation.connection(owner) as connection,
                connection.cursor() as cursor,
                pytest.raises((psycopg.errors.InsufficientPrivilege, pymysql.OperationalError)),
            ):
                cursor.execute(statement)
    assert interval_rows(foundation) == before


def test_query_does_not_hide_corrupt_stream_metadata_as_a_missing_interval(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    original = pending(foundation, recovery_ledger)
    recovery_ledger.append_interval(OutputIntervalRecord(interval(), original.identity))
    foundation.database.query(
        "UPDATE output_stream_heads SET sample_rate_hz = 16000 WHERE attempt_id = 'attempt-alt'"
    )
    with pytest.raises(SubmissionStoreError):
        recovery_ledger.query_output_interval("interval-a")


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_actual_commit_ack_loss_recovers_the_original_interval_without_automatic_retry(
    foundation: Foundation,
) -> None:
    pool = new_pool(foundation)
    try:
        ledger = PostgresSubmissionLedger(pool)
        original = pending(foundation, ledger)
        record = OutputIntervalRecord(interval(), original.identity)
        real = pool.connection
        calls = 0

        @contextmanager
        def lose_ack(*args: Any, **kwargs: Any) -> Iterator[Any]:
            nonlocal calls
            calls += 1
            with real(*args, **kwargs) as connection:
                yield connection
            raise psycopg.OperationalError("synthetic lost interval commit acknowledgment")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pool, "connection", lose_ack)
            with pytest.raises(SubmissionOutcomeUnknownError):
                ledger.append_interval(record)
        assert calls == 1
        before = ledger.get("intent-a")
        assert ledger.query_output_interval("interval-a") == record
        assert ledger.get("intent-a") == before
        assert ledger.append_interval(record).replayed
        assert ledger.get("intent-a") == before and len(interval_rows(foundation)) == 1
    finally:
        pool.close()
