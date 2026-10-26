"""Owned ingress/ASGI, restricted PG/InnoDB and real objects; inference is synthetic."""

import asyncio
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any, cast

import httpx
import psycopg
import pytest
from minio import Minio
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from speechnexus_contracts.execution import parse
from tests.fake.vad import ScriptedVadModel
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_asr_results import results
from tests.integration.test_gateway_frozen_inputs import (
    LIMITS,
    seed_file_asr_attempt,
    upload_command,
)
from tests.integration.test_gateway_frozen_inputs import object_store as object_store
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_stop_evidence import counters, new_pool
from tests.integration.test_gateway_submit_ledger import identity_for
from tests.integration.test_production_jobs import database as database
from tests.support.gateway_database import MySQLSubmissionLedger
from tests.unit.gateway.test_file_asr_command import CapturingRecognizer

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.command_dispatch import OriginalCommandDispatch
from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.file_execution import FileExecutionLedger, FileExecutionLimits
from speechnexus_gateway.domain.media import AudioDecodeLimits
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.submission import ExecutionState, SubmissionIntent, SubmitRequest
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.file_asr_runtime import AsrFileStage
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.interfaces.file_asr_command import CommandFileAsr
from speechnexus_gateway.settings import Settings

pytestmark = pytest.mark.integration
TOKEN = "synthetic-file-worker-component-token"


def original(foundation: Foundation, admin: Minio, bucket: str) -> SubmitRequest:
    seed_file_asr_attempt(foundation)
    command, _ = upload_command(admin, bucket)
    command["audio"]["storage"]["workspace_id"] = "workspace-test"
    command["parameters"]["max_segment_ms"] = 1000
    return SubmitRequest(
        identity=replace(
            identity_for(
                "lease-alt-stage-main",
                submit_intent_id="intent-a",
                sample_rate_hz=None,
                max_output_audio_ms=None,
            ),
            pool_id="asr-batch",
            instance_id="instance-file-asr",
            command_digest=command_digest(cast(JsonValue, command)),
        ),
        command=cast(JsonValue, command),
    )


def factory(
    inputs: MinioExecutionInputs,
    bucket: str,
    request: SubmitRequest,
    recognizer: CapturingRecognizer,
) -> Callable[[GatewayIngress, FileExecutionLedger], FileExecutionWorker[Any]]:
    recognizer.binding_revision_id = request.identity.binding_revision_id
    execution = CommandFileAsr(
        inputs=FileExecutionInputs(inputs),
        decoder=PyAvAudioDecoder(),
        vad_model=ScriptedVadModel(fallback=0.9),
        recognizer=recognizer,
        vad_tuning=VadPolicy(request.identity.policy_revision_id, max_segment_ms=1000),
        max_completion_tokens=64,
        max_call_seconds=2,
        input_limits=LIMITS,
        decode_limits=AudioDecodeLimits(),
    )
    store = results(bucket)

    def build(ingress: GatewayIngress, ledger: FileExecutionLedger) -> FileExecutionWorker[Any]:
        return FileExecutionWorker(
            ingress=ingress,
            ledger=ledger,
            execution=AsrFileStage(execution, 1024),
            results=store,
            limits=FileExecutionLimits(1024, 16384, 1, 10),
            engine_request_prefix="file-asr",
        )

    return build


async def receipt(client: httpx.AsyncClient) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            response = await client.get("/internal/v1/executions/intent-a/staging-result")
            assert response.status_code == 200, response.text
            value = parse("ExecutionStagingResultResponse", response.json())
            if value["staging_result"] is not None:
                return dict(value["staging_result"])
            await asyncio.sleep(0.01)


def worker_settings(foundation: Foundation, request: SubmitRequest, tmp_path: Path) -> Settings:
    locks = tmp_path / "ingress"
    locks.mkdir(mode=0o700)
    return Settings(
        control_database_url=SecretStr(
            make_conninfo(
                foundation.database.url,
                user=foundation.gateway_role,
                password="synthetic_foundation_test",
            )
        ),
        control_schema=foundation.database.schema,
        node_id=request.identity.host_node_id,
        gateway_id=request.identity.gateway_id,
        component_token=SecretStr(TOKEN),
        ingress_lock_directory=locks,
    )


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
async def test_owned_http_submit_runs_once_persists_result_and_restart_never_reruns(
    foundation: Foundation,
    object_store: tuple[MinioExecutionInputs, Minio, str],
    tmp_path: Path,
) -> None:
    inputs, admin, bucket = object_store
    request = await asyncio.to_thread(original, foundation, admin, bucket)
    recognizer = CapturingRecognizer()
    build = factory(inputs, bucket, request, recognizer)
    settings = worker_settings(foundation, request, tmp_path)
    before = await asyncio.to_thread(counters, foundation)
    headers = {"Authorization": "Bearer " + TOKEN}
    app = create_app(settings=settings, file_execution_factory=build)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        response = await client.post(
            "/internal/v1/executions/submit",
            json=cast(dict[str, Any], request.effective_request)["execution_request"],
        )
        assert response.status_code == 200, response.text
        accepted = parse("SubmitExecutionResponse", response.json())
        assert accepted["idempotent_replay"] is False
        assert accepted["execution_state"] == "executing"
        saved = await receipt(client)
        replay = await client.post(
            "/internal/v1/executions/submit",
            json=cast(dict[str, Any], request.effective_request)["execution_request"],
        )
        assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
        assert len(recognizer.requests) == 2
        assert saved["request_digest"] == request.request_digest
        assert saved["storage_version"] not in {"", "null"}
    assert app.state.file_execution_runtime is None
    restarted = create_app(settings=settings, file_execution_factory=build)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test", headers=headers
        ) as client,
    ):
        assert await receipt(client) == saved
        replay = await client.post(
            "/internal/v1/executions/submit",
            json=cast(dict[str, Any], request.effective_request)["execution_request"],
        )
        assert replay.status_code == 200
        assert replay.json()["execution_state"] == "unknown_quarantined"
        assert replay.json()["engine_request_id"] == accepted["engine_request_id"]
        assert len(recognizer.requests) == 2
    assert await asyncio.to_thread(counters, foundation) == before
    rows = await asyncio.to_thread(
        foundation.database.query, "SELECT * FROM execution_stop_evidence"
    )
    assert rows == []

    def stored_versions() -> list[Any]:
        return list(
            admin.list_objects(
                bucket, prefix="private/execution-results/", recursive=True, include_version=True
            )
        )

    assert len(await asyncio.to_thread(stored_versions)) == 1


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("phase", ["acceptance", "result"])
async def test_worker_recovers_actual_commits_after_lost_ack_without_repeating_work(
    foundation: Foundation,
    object_store: tuple[MinioExecutionInputs, Minio, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    inputs, admin, bucket = object_store
    request = await asyncio.to_thread(original, foundation, admin, bucket)
    recognizer = CapturingRecognizer()
    build = factory(inputs, bucket, request, recognizer)
    settings = worker_settings(foundation, request, tmp_path)
    fault_finished = Event()
    lost_commits = 0

    def with_fault(
        ingress: GatewayIngress, ledger: FileExecutionLedger
    ) -> FileExecutionWorker[Any]:
        assert isinstance(ledger, PostgresSubmissionLedger)
        record = ledger.record_execution_observation
        pool = ledger._pool
        connection = pool.connection

        @contextmanager
        def committed_without_ack(*args: Any, **kwargs: Any) -> Iterator[Any]:
            nonlocal lost_commits
            with connection(*args, **kwargs) as active:
                yield active
            lost_commits += 1  # The restricted-role PG transaction really committed.
            raise psycopg.OperationalError("synthetic lost file-worker commit acknowledgment")

        def record_with_fault(observation: RecordExecutionObservation) -> Any:
            result_write = observation.staging_result is not None
            if lost_commits or result_write != (phase == "result"):
                return record(observation)
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(pool, "connection", committed_without_ack)
                    return record(observation)
            finally:
                # No concurrent HTTP read uses the temporarily faulted connection.
                fault_finished.set()

        monkeypatch.setattr(ledger, "record_execution_observation", record_with_fault)
        return build(ingress, ledger)

    before = await asyncio.to_thread(counters, foundation)
    body = cast(dict[str, Any], request.effective_request)["execution_request"]
    headers = {"Authorization": "Bearer " + TOKEN}
    app = create_app(settings=settings, file_execution_factory=with_fault)
    saved: dict[str, Any] | None = None
    engine_id: str | None = None
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        response = await client.post("/internal/v1/executions/submit", json=body)
        assert response.status_code == (503 if phase == "acceptance" else 200), response.text
        if phase == "acceptance":
            error = parse("ExecutionControlErrorResponse", response.json())["error"]
            assert error["code"] == "control_outcome_unknown"
            assert error["recovery"] == "query_original" and error["retryable"] is False
        assert await asyncio.to_thread(fault_finished.wait, 3)
        assert lost_commits == 1
        for _ in range(2):
            replay = await client.post("/internal/v1/executions/submit", json=body)
            assert replay.status_code == 200, replay.text
            value = parse("SubmitExecutionResponse", replay.json())
            assert value["idempotent_replay"] is True
            assert value["execution_state"] == "executing"
            engine_id = value["engine_request_id"]
        if phase == "result":
            saved = await receipt(client)
        else:
            result = await client.get("/internal/v1/executions/intent-a/staging-result")
            assert result.status_code == 200 and result.json()["staging_result"] is None
        assert len(recognizer.requests) == (2 if phase == "result" else 0)

    # A new owned app can only quarantine and read the original identity/result.
    restarted = create_app(settings=settings, file_execution_factory=build)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test", headers=headers
        ) as client,
    ):
        replay = await client.post("/internal/v1/executions/submit", json=body)
        assert replay.status_code == 200, replay.text
        value = parse("SubmitExecutionResponse", replay.json())
        assert value["idempotent_replay"] is True
        assert value["execution_state"] == "unknown_quarantined"
        assert value["engine_request_id"] == engine_id
        if saved is not None:
            assert await receipt(client) == saved
        else:
            result = await client.get("/internal/v1/executions/intent-a/staging-result")
            assert result.status_code == 200 and result.json()["staging_result"] is None
    assert len(recognizer.requests) == (2 if phase == "result" else 0)
    assert lost_commits == 1
    versions = await asyncio.to_thread(
        lambda: list(
            admin.list_objects(
                bucket, prefix="private/execution-results/", recursive=True, include_version=True
            )
        )
    )
    assert len(versions) == (1 if phase == "result" else 0)
    assert await asyncio.to_thread(counters, foundation) == before
    assert (
        await asyncio.to_thread(foundation.database.query, "SELECT * FROM execution_stop_evidence")
        == []
    )


class ObservedSendLock:
    """Retain real mutual exclusion and observe an actual blocked acquisition."""

    def __init__(self) -> None:
        self.contended = Event()
        self._lock = Lock()

    def acquire(self, *, timeout: float) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        self.contended.set()
        return self._lock.acquire(timeout=timeout)

    def release(self) -> None:
        self._lock.release()


@pytest.mark.parametrize("winner", [0, 1])
async def test_two_independent_consumers_of_the_original_intent_execute_and_put_once(
    foundation: Foundation,
    object_store: tuple[MinioExecutionInputs, Minio, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winner: int,
) -> None:
    inputs, admin, bucket = object_store
    request = await asyncio.to_thread(original, foundation, admin, bucket)
    before = await asyncio.to_thread(counters, foundation)
    recognizers = [CapturingRecognizer(), CapturingRecognizer()]
    prepared = Event()
    absent_reads = Barrier(2)
    send_lock = ObservedSendLock()
    dispatches = [0, 0]
    with ExitStack() as lifetime:
        # One legitimate process owner/ingress, two independent ledgers and executors.
        # A second process cannot claim this same gateway identity concurrently.
        ownership = ProcessIngressOwnership(
            tmp_path / "competing-ingress", node_id="host-test", gateway_id="gateway-test"
        )
        lifetime.callback(ownership.close)
        ingress = GatewayIngress(ownership, timeout_seconds=5)
        monkeypatch.setattr(ingress._pools[request.identity.pool_id], "sending", send_lock)
        ledgers: list[Ledger] = []
        for _ in range(2):
            if foundation.database.dialect == "postgresql":
                pool = new_pool(foundation)
                lifetime.callback(pool.close)
                ledgers.append(PostgresSubmissionLedger(pool))
            else:
                ledgers.append(MySQLSubmissionLedger(foundation))
        workers = [
            factory(inputs, bucket, request, recognizers[index])(ingress, ledger)
            for index, ledger in enumerate(ledgers)
        ]
        for worker in workers:
            worker.open()
            lifetime.callback(worker.close)

        def synchronize(index: int) -> None:
            ledger, worker = ledgers[index], workers[index]
            read, dispatch = ledger.get, worker.dispatch
            first_read = True

            def racing_read(key: str) -> SubmissionIntent | None:
                nonlocal first_read
                value = read(key)
                if first_read:
                    first_read = False
                    assert key == request.identity.submit_intent_id and value is None
                    absent_reads.wait(timeout=5)
                    if index != winner:
                        assert prepared.wait(5)
                return value

            def prepare_once(command: SubmitRequest) -> Any:
                dispatches[index] += 1
                outcome = dispatch(command)
                prepared.set()
                # The competing submit really waits on the same send lock before
                # the first consumer commits acceptance or can launch its file task.
                assert send_lock.contended.wait(5)
                return outcome

            monkeypatch.setattr(ledger, "get", racing_read)
            monkeypatch.setattr(worker, "dispatch", prepare_once)

        for index in range(2):
            synchronize(index)
        services = [
            SubmitExecutionService(
                ledger=ledger,
                ingress=ingress,
                dispatch=OriginalCommandDispatch(
                    commands=ledger, engine=worker, max_document_bytes=16384
                ),
                observation=worker,
                committed_execution=worker,
            )
            for ledger, worker in zip(ledgers, workers, strict=True)
        ]

        def compete() -> list[Any]:
            with ThreadPoolExecutor(max_workers=2) as consumers:
                pending = [consumers.submit(service.submit, request) for service in services]
                return [future.result(timeout=10) for future in pending]

        outcomes = await asyncio.to_thread(compete)
        assert send_lock.contended.is_set()
        assert [outcome.idempotent_replay for outcome in outcomes] == [
            index != winner for index in range(2)
        ]
        assert dispatches == [int(index == winner) for index in range(2)]
        engine_id = outcomes[winner].intent.engine_request_id
        assert engine_id is not None
        assert all(outcome.intent.engine_request_id == engine_id for outcome in outcomes)
        async with asyncio.timeout(5):
            while True:
                saved = await asyncio.to_thread(ledgers[0].get, "intent-a")
                assert saved is not None
                if saved.staging_result is not None:
                    break
                await asyncio.sleep(0.01)
        for service in services:
            replay = await asyncio.to_thread(service.submit, request)
            assert replay.idempotent_replay and replay.intent.staging_result == saved.staging_result
            assert replay.intent.engine_request_id == engine_id
        assert saved.execution_state is ExecutionState.EXECUTING
        assert saved.staging_result.request_digest == request.request_digest
        assert [len(recognizer.requests) for recognizer in recognizers] == [
            2 * int(index == winner) for index in range(2)
        ]
        assert dispatches == [int(index == winner) for index in range(2)]
    versions = await asyncio.to_thread(
        lambda: list(
            admin.list_objects(
                bucket, prefix="private/execution-results/", recursive=True, include_version=True
            )
        )
    )
    assert len(versions) == 1
    assert versions[0].version_id == saved.staging_result.storage_version
    assert await asyncio.to_thread(counters, foundation) == before
    assert (
        await asyncio.to_thread(foundation.database.query, "SELECT * FROM execution_stop_evidence")
        == []
    )
