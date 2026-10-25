"""Synthetic TTS, real codecs/MinIO/PG/InnoDB and authenticated receipt consumers.

No real model, production command factory, publication or stop qualification.
"""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from minio import Minio
from speechnexus_contracts.execution import parse
from tests.fake.execution import fake_ingress
from tests.fake.execution_command import command_example
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_frozen_inputs import object_store as object_store
from tests.integration.test_gateway_frozen_inputs import persist
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import counters, new_pool
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_production_jobs import database as database
from tests.support.gateway_database import MySQLSubmissionLedger
from tests.support.gateway_locks import wait_for_gateway_locks
from tests.unit.gateway.test_barrier_http import HEADERS, settings_for
from tests.unit.gateway.test_tts import AUDIO, FakeSynthesizer
from tests.unit.gateway.test_tts_results_minio import prepared

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.file_tts import FileTtsSynthesis
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.domain.execution_results import ExecutionAudioStagingResult
from speechnexus_gateway.domain.observation import ExecutionObservation, RecordExecutionObservation
from speechnexus_gateway.domain.output_intervals import (
    AppendInterval,
    MeterKind,
    OutputIntervalRecord,
)
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionState,
    InvalidExecutionRequestError,
    StaleSubmissionVersionError,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
)
from speechnexus_gateway.domain.tts import TtsDeliveryFormat, TtsPolicy, TtsRequest
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.infrastructure.tts_results_minio import MinioTtsResults

pytestmark = pytest.mark.integration


def start(
    f: Foundation, ledger: Ledger, target: TtsDeliveryFormat = TtsDeliveryFormat.WAV
) -> SubmissionIntent:
    command = command_example(0)
    command["parameters"]["response_format"] = target.value
    _, current = persist(f, ledger, command)
    current.mark_dispatched("synthetic-tts", at=datetime.now(UTC))
    return ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(current, expected_version=1)
    )


def generated(ledger: Ledger, current: SubmissionIntent, *, gap: bool = False) -> SubmissionIntent:
    ledger.append_interval(
        OutputIntervalRecord(
            AppendInterval(
                "generated-tts",
                current.identity.submit_intent_id,
                "tts-main",
                24000,
                MeterKind.GENERATED,
                100 if gap else 0,
                len(AUDIO.pcm16) // 2,
            ),
            current.identity,
        )
    )
    saved = ledger.get(current.identity.submit_intent_id)
    assert saved is not None
    return saved


def receipt(current: SubmissionIntent) -> ExecutionAudioStagingResult:
    result = replace(
        prepared(),
        workspace_id=current.identity.workspace_id,
        request_digest=current.request_digest,
    )
    return result.receipt("synthetic-private", "synthetic-version")


def registration(
    current: SubmissionIntent, result: ExecutionAudioStagingResult | None = None
) -> RecordExecutionObservation:
    return RecordExecutionObservation(
        ExecutionObservation.from_intent(current),
        current.version,
        receipt(current) if result is None else result,
    )


@pytest.mark.parametrize("target", list(TtsDeliveryFormat))
async def test_synthesized_audio_through_real_storage_meter_and_authenticated_durable_consumer(
    foundation: Foundation,
    recovery_ledger: Ledger,
    object_store: tuple[MinioExecutionInputs, Minio, str],
    tmp_path: Path,
    target: TtsDeliveryFormat,
) -> None:
    _, admin, bucket = object_store
    current = start(foundation, recovery_ledger, target)
    policy = TtsPolicy(current.identity.policy_revision_id, "Vivian")
    engine = FakeSynthesizer(
        audio=replace(AUDIO, request_id="synthetic-tts", policy_digest=policy.digest),
        binding_revision_id=current.identity.binding_revision_id,
    )
    synthesis = FileTtsSynthesis(
        synthesizer=engine,
        encoder=LocalTtsEncoder(),
        request=TtsRequest("synthetic-tts", "Synthetic narration."),
        policy=policy,
        delivery_format=target,
        max_payload_bytes=1024 * 1024,
        cancelled=lambda: False,
        execution_id=current.identity.submit_intent_id,
    )
    store = MinioTtsResults(
        endpoint=os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"],
        bucket=bucket,
        access_key=os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"],
        timeout_seconds=10,
    )
    before = counters(foundation)
    result = await synthesis.stage_result(
        workspace_id=current.identity.workspace_id,
        request_digest=current.request_digest,
        store=store,
    )
    current = generated(recovery_ledger, current)
    write = registration(current, result)
    saved = recovery_ledger.record_execution_observation(write)
    assert (
        recovery_ledger.record_execution_observation(replace(write, expected_version=saved.version))
        == saved
    )
    # Reconstruct from a separate actual repository, not a coroutine-local receipt.
    pool = new_pool(foundation) if foundation.database.dialect == "postgresql" else None
    try:
        reader = (
            PostgresSubmissionLedger(pool)
            if pool is not None
            else MySQLSubmissionLedger(foundation)
        )
        recovered = reader.query_outcome(saved.identity.submit_intent_id)
        assert recovered is not None and recovered.intent == saved
        ingress = fake_ingress(node_id="host-test", gateway_id="gateway-test")
        app = create_app(
            settings=settings_for(tmp_path, node_id="host-test", gateway_id="gateway-test"),
            barrier_control=GatewayBarrierService(ingress=ingress, ledger=reader),
            execution_query=ExecutionQueryService(ingress=ingress, ledger=reader),
        )
        base = f"/internal/v1/executions/{saved.identity.submit_intent_id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(base + "/audio-staging-result")).status_code == 401
            response = await client.get(base + "/audio-staging-result", headers=HEADERS)
            assert (
                response.status_code == 200 and response.json()["staging_result"] == result.value()
            )
            parse("ExecutionAudioStagingResultResponse", response.json())
            old = await client.get(base + "/staging-result", headers=HEADERS)
            parse("ExecutionStagingResultResponse", old.json())
            assert old.json()["staging_result"] is None
        assert reader.get(saved.identity.submit_intent_id) == saved
    finally:
        if pool is not None:
            pool.close()
    versions = list(
        admin.list_objects(
            bucket, prefix=result.reference.object_key, recursive=True, include_version=True
        )
    )
    assert len(versions) == 1 and versions[0].version_id == result.storage_version
    assert len(engine.calls) == 1
    assert saved.generated_pcm_samples == result.audio.source_pcm_samples
    assert saved.execution_state is ExecutionState.EXECUTING
    assert saved.stop_evidence_digest is None and saved.terminated_at is None
    assert counters(foundation) == before


@pytest.mark.parametrize("facts", ["missing", "gap", "wrong-watermark"])
def test_registration_checks_real_generated_intervals_not_just_max_end(
    foundation: Foundation,
    recovery_ledger: Ledger,
    facts: str,
) -> None:
    current = start(foundation, recovery_ledger)
    if facts != "missing":
        current = generated(recovery_ledger, current, gap=facts == "gap")
    result = receipt(current)
    if facts == "wrong-watermark":
        result = replace(
            result, audio=replace(result.audio, source_pcm_samples=2376, duration_ms=99)
        )
    before = counters(foundation)
    with pytest.raises(InvalidExecutionRequestError):
        recovery_ledger.record_execution_observation(registration(current, result))
    assert recovery_ledger.get(current.identity.submit_intent_id) == current
    assert counters(foundation) == before


def test_two_audio_registrations_really_contend_for_the_original_intent(
    foundation: Foundation,
    recovery_ledger: Ledger,
) -> None:
    current = generated(recovery_ledger, start(foundation, recovery_ledger))
    write = registration(current)
    before = counters(foundation)

    def register() -> str:
        try:
            recovery_ledger.record_execution_observation(write)
            return "committed"
        except StaleSubmissionVersionError:
            return "stale"

    with foundation.database.blocker() as blocker:
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT submit_intent_id FROM submit_intents "
                "WHERE submit_intent_id = %s FOR UPDATE",
                (current.identity.submit_intent_id,),
            )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(register) for _ in range(2)]
            try:
                wait_for_gateway_locks(foundation, "submit_intents")
            finally:
                blocker.rollback()
            assert sorted(future.result(timeout=5) for future in futures) == ["committed", "stale"]
    saved = recovery_ledger.get(current.identity.submit_intent_id)
    assert saved is not None and saved.version == current.version + 1
    assert saved.staging_result == write.staging_result
    assert counters(foundation) == before


@pytest.mark.parametrize("registered", [False, True])
def test_cancel_rejects_new_audio_but_exact_receipt_replay_preserves_later_cancel(
    foundation: Foundation,
    recovery_ledger: Ledger,
    registered: bool,
) -> None:
    current = generated(recovery_ledger, start(foundation, recovery_ledger))
    saved = (
        recovery_ledger.record_execution_observation(registration(current))
        if registered
        else current
    )
    stopping = recovery_ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(
            replace(saved, execution_state=ExecutionState.STOPPING),
            expected_version=saved.version,
        )
    )
    before = counters(foundation)
    write = replace(registration(current), expected_version=stopping.version)
    if registered:
        assert recovery_ledger.record_execution_observation(write) == stopping
    else:
        with pytest.raises(ExecutionCleanupPendingError):
            recovery_ledger.record_execution_observation(write)
    assert counters(foundation) == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_audio_commit_ack_loss_is_recovered_by_original_identity_without_reencoding(
    foundation: Foundation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = new_pool(foundation)
    try:
        ledger = PostgresSubmissionLedger(pool)
        current = generated(ledger, start(foundation, ledger))
        write = registration(current)
        original = pool.connection
        before = counters(foundation)

        @contextmanager
        def lose_ack(*args: Any, **kwargs: Any) -> Iterator[Any]:
            with original(*args, **kwargs) as connection:
                yield connection
            raise psycopg.OperationalError("synthetic lost audio receipt acknowledgment")

        with monkeypatch.context() as patch:
            patch.setattr(pool, "connection", lose_ack)
            with pytest.raises(SubmissionOutcomeUnknownError):
                ledger.record_execution_observation(write)
        recovered = ledger.query_outcome(current.identity.submit_intent_id)
        assert recovered is not None and recovered.intent.staging_result == write.staging_result
        assert (
            ledger.record_execution_observation(
                replace(write, expected_version=recovered.intent.version)
            )
            == recovered.intent
        )
        assert counters(foundation) == before
    finally:
        pool.close()
