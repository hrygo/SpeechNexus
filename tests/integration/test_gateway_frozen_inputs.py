"""Original gateway command reads on PG/InnoDB and versioned object consumption.

The recognizer/VAD are synthetic. These tests prove input plumbing, never vendor
execution/stop facts, model qualification, A's release transaction or publication.
"""

import asyncio
import hashlib
import io
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from minio import Minio
from minio.commonconfig import ENABLED
from minio.versioningconfig import VersioningConfig
from tests.fake.execution_command import command_example
from tests.fake.vad import ScriptedVadModel
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import counters
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import identity_for, seed_second_attempt
from tests.integration.test_production_jobs import database as database
from tests.unit.gateway.test_audio_decode import wav_source
from tests.unit.gateway.test_file_asr import Recognizer
from tests.unit.gateway.test_original_command import Engine

from speechnexus_gateway.application.command_dispatch import OriginalCommandDispatch
from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.domain.asr import AsrPolicy
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputError,
    ExecutionInputLimits,
    VerifiedExecutionObject,
)
from speechnexus_gateway.domain.media import AudioDecodeLimits
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    StaleSubmissionVersionError,
    SubmissionIntent,
    SubmissionStoreError,
    SubmitRequest,
)
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_gateway.interfaces.file_asr_command import CommandFileAsr

pytestmark = pytest.mark.integration
LIMITS = ExecutionInputLimits(1000000, 1024, 10)


def persist(
    foundation: Foundation, ledger: Ledger, command: dict[str, Any] | None = None
) -> tuple[SubmitRequest, SubmissionIntent]:
    seed_second_attempt(foundation)
    value = command_example() if command is None else command
    emits_audio = value["kind"] == "tts"
    if not emits_audio:
        value["audio"]["storage"]["workspace_id"] = "workspace-test"
    identity = replace(
        identity_for(
            "lease-alt-stage-main",
            submit_intent_id="intent-a",
            sample_rate_hz=24000 if emits_audio else None,
            max_output_audio_ms=2000 if emits_audio else None,
        ),
        command_digest=command_digest(cast(JsonValue, value)),
    )
    request = SubmitRequest(
        identity=identity,
        command=cast(JsonValue, value),
        output_stream_id="tts-main" if emits_audio else None,
    )
    now = datetime.now(UTC)
    intent = SubmissionIntent(
        identity=identity,
        request_digest=request.request_digest,
        output_stream_id=request.output_stream_id,
        recorded_at=now,
        updated_at=now,
    )
    ledger.add(intent, request_document=request.request_document)
    return request, intent


def snapshot(foundation: Foundation) -> list[Any]:
    return [
        foundation.database.query("SELECT * FROM submit_intents ORDER BY submit_intent_id"),
        foundation.database.query("SELECT * FROM execution_stop_evidence ORDER BY evidence_id"),
        foundation.database.query("SELECT * FROM output_sample_intervals ORDER BY interval_id"),
        counters(foundation),
    ]


def test_original_request_reads_as_gateway_without_mutation_or_reconstruction(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    request, intent = persist(foundation, recovery_ledger)
    before = snapshot(foundation)
    for _ in range(2):
        actual = recovery_ledger.read_request(intent, max_document_bytes=16384)
        assert actual == request and actual.request_document == request.request_document
    assert snapshot(foundation) == before


def test_multibyte_command_over_64k_uses_byte_budget_and_original_pcm_identity(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    command = command_example(0)
    command["text"] = "合成" * 20000
    request, intent = persist(foundation, recovery_ledger, command)
    size = len(request.request_document.encode())
    assert size > 65536
    before = snapshot(foundation)
    with pytest.raises(SubmissionStoreError):
        recovery_ledger.read_request(intent, max_document_bytes=size - 1)
    actual = recovery_ledger.read_request(intent, max_document_bytes=size)
    assert actual.request_document == request.request_document
    assert actual.output_stream_id == "tts-main" and actual.identity.sample_rate_hz == 24000
    assert snapshot(foundation) == before


@pytest.mark.parametrize(
    "change",
    ["too-small", "digest", "identity", "pretty", "duplicate", "schema", "redacted", "stale"],
)
def test_unverifiable_request_never_dispatches_or_changes_budget(
    foundation: Foundation, recovery_ledger: Ledger, change: str
) -> None:
    request, intent = persist(foundation, recovery_ledger)
    if change not in {"too-small", "stale", "redacted"}:
        value = json.loads(request.request_document)
        if change == "digest":
            value["execution_request"]["command"]["parameters"]["language"] = "zh"
        if change == "identity":
            value["execution_request"]["instance_generation"] = 2
        if change == "schema":
            del value["execution_request"]["command"]["parameters"]["language"]
        document = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if change == "pretty":
            document = json.dumps(value, indent=2)
        if change == "duplicate":
            document = '{"output_stream_id":null,' + document[1:]
        foundation.database.query(
            "UPDATE submit_intents SET request_document = %s WHERE submit_intent_id = 'intent-a'",
            (document,),
        )
    elif change == "stale":
        foundation.database.query(
            "UPDATE submit_intents SET version = version + 1 WHERE submit_intent_id = 'intent-a'"
        )
    elif change == "redacted":
        foundation.database.query(
            "UPDATE submit_intents SET request_document = NULL, command_redacted_at_us = 1, "
            "command_deletion_job_id = 'delete-input', command_deletion_evidence_digest = %s "
            "WHERE submit_intent_id = 'intent-a'",
            ("sha256:" + "d" * 64,),
        )
    before = snapshot(foundation)
    limit = 1 if change == "too-small" else 16384
    expected = StaleSubmissionVersionError if change == "stale" else SubmissionStoreError
    with pytest.raises(expected):
        recovery_ledger.read_request(intent, max_document_bytes=limit)
    engine = Engine()
    bridge = OriginalCommandDispatch(
        commands=recovery_ledger, engine=engine, max_document_bytes=limit
    )
    assert bridge.dispatch(intent).result is DispatchResult.NOT_SENT and not engine.calls
    assert snapshot(foundation) == before


@pytest.fixture
def object_store(tmp_path: Path) -> Iterator[tuple[MinioExecutionInputs, Minio, str]]:
    values = [
        os.environ.get("SPEECHNEXUS_TEST_MINIO_" + key)
        for key in ("ENDPOINT", "ACCESS_KEY", "SECRET_KEY")
    ]
    if not all(values):
        pytest.fail("Gateway input tests require isolated MinIO and explicit test keys")
    endpoint, access, secret = cast(tuple[str, str, str], tuple(values))
    assert endpoint.startswith("http://127.0.0.1:"), "Use only the isolated local test service"
    admin = Minio(
        endpoint.removeprefix("http://"),
        access_key=access,
        secret_key=secret,
        secure=False,
        region="us-east-1",
    )
    bucket = "sn-gateway-input-" + uuid.uuid4().hex
    admin.make_bucket(bucket)
    admin.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
    tmp_path.chmod(0o700)
    objects = MinioExecutionInputs(
        endpoint=endpoint,
        bucket=bucket,
        access_key=access,
        secret_key=secret,
        spool_directory=tmp_path,
    )
    try:
        yield objects, admin, bucket
    finally:
        objects.close()
        for item in admin.list_objects(bucket, recursive=True, include_version=True):
            admin.remove_object(bucket, item.object_name, version_id=item.version_id)
        admin.remove_bucket(bucket)


def upload_command(admin: Minio, bucket: str) -> tuple[dict[str, Any], bytes]:
    with wav_source(samples=48013, channels=1) as stream:
        body = stream.read()
    key = "private/assets/asset-input/" + uuid.uuid4().hex
    admin.put_object(bucket, key, io.BytesIO(body), len(body), content_type="audio/wav")
    command = command_example()
    command["audio"].update({"sample_rate_hz": 48000, "channels": 1})
    command["audio"]["storage"].update(
        {
            "storage_bucket": bucket,
            "object_key": key,
            "byte_length": len(body),
            "content_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
        }
    )
    return command, body


@pytest.mark.parametrize("bad_digest", [False, True])
async def test_persisted_command_minio_decoder_and_native_asr_consumer(
    foundation: Foundation,
    recovery_ledger: Ledger,
    object_store: tuple[MinioExecutionInputs, Minio, str],
    bad_digest: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects, admin, bucket = object_store
    command, _ = upload_command(admin, bucket)
    command["parameters"].update(
        {
            "language": "zh",
            "prompt": "Synthetic frozen hint",
            "temperature": 0.25,
            "max_segment_ms": 1000,
            "final_deadline_ms": 600,
            "turn_detection": {"mode": "server_vad", "silence_duration_ms": 240},
        }
    )
    if bad_digest:
        command["audio"]["storage"]["content_sha256"] = "sha256:" + "0" * 64
    request, intent = persist(foundation, recovery_ledger, command)
    restored = recovery_ledger.read_request(intent, max_document_bytes=16384)
    assert restored.request_document == request.request_document
    before = snapshot(foundation)
    recognizer = Recognizer()
    recognizer.binding_revision_id = restored.identity.binding_revision_id
    opened: list[VerifiedExecutionObject] = []
    original_open = objects.open_verified

    @asynccontextmanager
    async def track_open(*args: Any, **kwargs: Any) -> AsyncIterator[VerifiedExecutionObject]:
        async with original_open(*args, **kwargs) as verified:
            opened.append(verified)
            yield verified

    monkeypatch.setattr(objects, "open_verified", track_open)
    consumer = CommandFileAsr(
        inputs=FileExecutionInputs(objects),
        decoder=PyAvAudioDecoder(),
        vad_model=ScriptedVadModel(fallback=0.9),
        recognizer=recognizer,
        vad_tuning=VadPolicy(restored.identity.policy_revision_id),
        max_completion_tokens=64,
        max_call_seconds=2,
        input_limits=LIMITS,
        decode_limits=AudioDecodeLimits(),
    )
    expected_policy = AsrPolicy(
        restored.identity.policy_revision_id,
        language="zh",
        prompt="Synthetic frozen hint",
        temperature=0.25,
        max_completion_tokens=64,
        timeout_seconds=0.6,
    )
    expected_vad = VadPolicy(
        restored.identity.policy_revision_id, max_segment_ms=1000, silence_duration_ms=240
    )
    if bad_digest:
        with pytest.raises(ExecutionInputError, match="identity_mismatch"):
            async with consumer.open(restored, cancelled=lambda: False):
                pytest.fail("corrupt content reached decoder")
        assert not recognizer.requests and not opened
    else:
        async with consumer.open(restored, cancelled=lambda: False) as transcription:
            result = [segment async for segment in transcription]
            assert (
                transcription.summary is not None and transcription.summary.source_samples == 48013
            )
            assert len(result) == len(recognizer.requests) == 2
            assert result[-1].source_sample_span[1] == 48013
            assert all(s.policy_digest == expected_policy.digest for s in result)
            assert all(s.vad_policy_digest == expected_vad.digest for s in result)
        assert opened and all(verified.stream.closed for verified in opened)
    assert snapshot(foundation) == before


async def test_overwrite_between_stat_and_get_reads_pinned_original(
    object_store: tuple[MinioExecutionInputs, Minio, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    objects, admin, bucket = object_store
    command, body = await asyncio.to_thread(upload_command, admin, bucket)
    command["audio"]["storage"]["workspace_id"] = "ws-demo"
    from tests.fake.execution_command import original_request

    original_head = objects._head
    versions: list[str] = []

    async def head(*args: Any, **kwargs: Any) -> str:
        version = await original_head(*args, **kwargs)
        versions.append(version)
        await asyncio.to_thread(
            admin.put_object,
            bucket,
            command["audio"]["storage"]["object_key"],
            io.BytesIO(b"replacement"),
            11,
        )
        return version

    monkeypatch.setattr(objects, "_head", head)
    async with FileExecutionInputs(objects).open_asr(
        original_request(command), limits=LIMITS, cancelled=lambda: False
    ) as prepared:
        assert prepared.object_input.stream.read() == body
        assert prepared.object_input.storage_version == versions[0]
    monkeypatch.setattr(objects, "_head", original_head)
    with pytest.raises(ExecutionInputError, match="identity_mismatch"):
        async with FileExecutionInputs(objects).open_asr(
            original_request(command), limits=LIMITS, cancelled=lambda: False
        ):
            pytest.fail("replacement accepted")


@pytest.mark.parametrize(
    "suffix",
    [
        "with space",
        "中文音频",
        "literal%2F%20%25",
        "%2e%2e/%252F",
        "plus+equals=and&percent%",
        "cafe\u0301",
    ],
)
async def test_async_input_signing_preserves_opaque_object_keys(
    object_store: tuple[MinioExecutionInputs, Minio, str], suffix: str
) -> None:
    from speechnexus_gateway.domain.execution_inputs import ExecutionObjectReference

    objects, admin, bucket = object_store
    body = b"synthetic opaque-key content"
    key = "private/assets/asset-input/" + suffix
    created = await asyncio.to_thread(
        admin.put_object, bucket, key, io.BytesIO(body), len(body), content_type="audio/wav"
    )
    ref = ExecutionObjectReference(
        "asset-input",
        "ws-demo",
        bucket,
        key,
        "sha256:" + hashlib.sha256(body).hexdigest(),
        len(body),
    )
    async with objects.open_verified(ref, limits=LIMITS, cancelled=lambda: False) as verified:
        assert verified.stream.read() == body
        assert verified.storage_version == created.version_id
    assert verified.stream.closed
