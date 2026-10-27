"""Default factory with PG/MinIO and native TCP; only VAD/model inference is scripted."""

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from minio import Minio
from pydantic import SecretStr
from tests.fake.file_asr_configuration import write_configuration
from tests.fake.vad import ScriptedVadModel
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_file_worker import TOKEN, original, receipt, worker_settings
from tests.integration.test_gateway_frozen_inputs import object_store as object_store
from tests.integration.test_gateway_stop_evidence import counters
from tests.integration.test_production_jobs import database as database
from tests.unit.gateway.test_vllm_asr import response_bytes

from speechnexus_gateway.app import create_app
from speechnexus_gateway.domain.asr import AsrExecutionTarget
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputLimits,
    ExecutionObjectReference,
)
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from speechnexus_gateway.settings import Settings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("speech", [True, False])
async def test_default_entrypoint_consumes_original_command_and_never_reruns_after_restart(
    foundation: Foundation,
    object_store: tuple[MinioExecutionInputs, Minio, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    speech: bool,
) -> None:
    inputs, admin, bucket = object_store
    request = await asyncio.to_thread(original, foundation, admin, bucket)
    calls: list[tuple[bytes, bytes]] = []
    server_errors: list[Exception] = []
    handlers: set[asyncio.Task[Any]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            async with asyncio.timeout(5):
                headers = await reader.readuntil(b"\r\n\r\n")
                values = dict(
                    line.lower().split(b":", 1)
                    for line in headers.split(b"\r\n")[1:]
                    if b":" in line
                )
                body = await reader.readexactly(int(values[b"content-length"].strip()))
                calls.append((headers, body))
                correlation = values[b"x-request-id"].strip()
                content = response_bytes(text="Synthetic.").replace(
                    b"transcribe-request-1", b"transcribe-" + correlation
                )
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nX-Request-ID: "
                    + correlation
                    + f"\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode()
                    + content
                )
                await writer.drain()
        except Exception as error:
            server_errors.append(error)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            handlers.discard(task)

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        identity = request.identity
        path, _ = write_configuration(
            tmp_path / "deployment",
            target=AsrExecutionTarget(
                identity.host_node_id,
                identity.gateway_id,
                identity.pool_id,
                identity.instance_id,
                identity.instance_generation,
                identity.binding_revision_id,
            ),
            policy_revision=identity.policy_revision_id,
            port=server.sockets[0].getsockname()[1],
            object_endpoint=os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"],
            bucket=bucket,
        )
        settings = Settings(
            **(
                worker_settings(foundation, request, tmp_path).model_dump()
                | {
                    "file_asr_config_file": path,
                    "file_asr_object_access_key": SecretStr(
                        os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"]
                    ),
                    "file_asr_object_secret_key": SecretStr(
                        os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"]
                    ),
                }
            )
        )
        monkeypatch.setattr(
            "speechnexus_gateway.file_asr_runtime.SileroOnnxCpuModel",
            lambda **_: ScriptedVadModel(fallback=0.9 if speech else 0.0),
        )
        before = await asyncio.to_thread(counters, foundation)
        body = cast(dict[str, Any], request.effective_request)["execution_request"]
        app = create_app(settings=settings)  # Deliberately no injected factory/services.
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer " + TOKEN},
            ) as client,
        ):
            assert (await client.get("/readyz")).status_code == 200
            accepted = await client.post("/internal/v1/executions/submit", json=body)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["idempotent_replay"] is False
            saved = await receipt(client)
            replay = await client.post("/internal/v1/executions/submit", json=body)
            assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
            assert len(calls) == (2 if speech else 0)
            reference = ExecutionObjectReference(**saved["reference"])
            async with inputs.open_verified(
                reference,
                limits=ExecutionInputLimits(1024, 64, 5),
                cancelled=lambda: False,
            ) as verified:
                assert verified.storage_version == saved["storage_version"]
                document = json.loads(verified.stream.read())
                # duration_ms is the measured audio duration, not an engine claim;
                # it is always present and must be positive.
                assert document.pop("duration_ms") > 0
                assert document == {"text": "Synthetic.\nSynthetic." if speech else ""}
        assert app.state.file_execution_runtime is app.state.submit_execution is None
        restarted = create_app(settings=settings)
        async with (
            restarted.router.lifespan_context(restarted),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted),
                base_url="http://test",
                headers={"Authorization": "Bearer " + TOKEN},
            ) as client,
        ):
            assert await receipt(client) == saved
            replay = await client.post("/internal/v1/executions/submit", json=body)
            assert replay.status_code == 200
            assert replay.json()["execution_state"] == "unknown_quarantined"
            assert replay.json()["engine_request_id"] == accepted.json()["engine_request_id"]
            assert len(calls) == (2 if speech else 0)
        await asyncio.gather(*handlers)
        assert not server_errors
        for headers, native_body in calls:
            assert headers.startswith(b"POST /v1/audio/transcriptions HTTP/1.1\r\n")
            assert b"Authorization: Bearer " + b"a" * 64 + b"\r\n" in headers
            assert b'name="max_completion_tokens"\r\n\r\n64' in native_body
        assert await asyncio.to_thread(counters, foundation) == before
        assert (
            await asyncio.to_thread(
                foundation.database.query, "SELECT * FROM execution_stop_evidence"
            )
            == []
        )
        versions = await asyncio.to_thread(
            lambda: list(
                admin.list_objects(
                    bucket,
                    prefix="private/execution-results/",
                    recursive=True,
                    include_version=True,
                )
            )
        )
        assert len(versions) == 1
