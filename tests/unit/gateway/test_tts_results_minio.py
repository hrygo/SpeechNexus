"""Encoded audio staging uses one private, bounded PUT and fixed-version readback."""

import asyncio
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest

from speechnexus_gateway.application.file_tts import FileTtsSynthesis
from speechnexus_gateway.domain.tts import TtsDeliveryError, TtsDeliveryFormat, TtsRequest
from speechnexus_gateway.domain.tts_results import PreparedTtsResult, TtsResultError
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.infrastructure.tts_results_minio import MinioTtsResults
from tests.unit.gateway.test_asr_results_deadline import Server
from tests.unit.gateway.test_asr_results_deadline import server as server
from tests.unit.gateway.test_tts import AUDIO, DELIVERY, POLICY, FakeEncoder, FakeSynthesizer, build


def prepared(target: TtsDeliveryFormat = TtsDeliveryFormat.WAV) -> PreparedTtsResult:
    delivery = LocalTtsEncoder().encode(
        AUDIO, target=target, max_payload_bytes=1024 * 1024, cancelled=lambda: False
    )
    return PreparedTtsResult.from_delivery(
        workspace_id="workspace-one",
        request_digest="sha256:" + "a" * 64,
        delivery=delivery,
        source_pcm_samples=len(AUDIO.pcm16) // 2,
    )


def store() -> MinioTtsResults:
    return MinioTtsResults(
        endpoint="http://127.0.0.1:59000",
        bucket="sn-audio-test",
        access_key="synthetic",
        secret_key="synthetic",
        timeout_seconds=2,
    )


@pytest.mark.parametrize("target", list(TtsDeliveryFormat))
async def test_audio_delivery_is_versioned_and_read_back_without_relabeling(
    monkeypatch: pytest.MonkeyPatch, target: TtsDeliveryFormat
) -> None:
    result = prepared(target)
    value = store()
    calls: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            assert request.url.params["versionId"] == "audio-v1"
        return httpx.Response(
            200,
            headers={
                "x-amz-version-id": "audio-v1",
                "Content-Length": str(len(result.body) if request.method != "PUT" else 0),
            },
            stream=httpx.ByteStream(result.body if request.method == "GET" else b""),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(value, "_new_client", lambda: client)
    receipt = await value.stage(result, cancelled=lambda: False)
    assert [call.method for call in calls] == ["PUT", "HEAD", "GET"]
    assert calls[0].headers["Content-Type"] == result.media_type
    assert calls[0].content == result.body
    assert receipt == result.receipt("sn-audio-test", "audio-v1")
    assert client.is_closed


@pytest.mark.parametrize("failure", ["ack-lost", "wrong-digest", "wrong-version"])
async def test_audio_storage_failure_never_retries_or_returns_a_receipt(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    result = prepared()
    value = store()
    calls = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "PUT" and failure == "ack-lost":
            raise httpx.ReadTimeout("private vendor detail")
        return httpx.Response(
            200,
            headers={
                "x-amz-version-id": "v2"
                if failure == "wrong-version" and request.method != "PUT"
                else "v1",
                "Content-Length": str(len(result.body) if request.method != "PUT" else 0),
            },
            stream=httpx.ByteStream(b"x" * len(result.body) if request.method == "GET" else b""),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(value, "_new_client", lambda: client)
    with pytest.raises(TtsResultError) as error:
        await value.stage(result, cancelled=lambda: False)
    assert (
        error.value.code
        == {
            "ack-lost": "tts_result_storage_unknown",
            "wrong-digest": "tts_result_readback_failed",
            "wrong-version": "tts_result_identity_mismatch",
        }[failure]
    )
    assert calls.count("PUT") == 1
    assert client.is_closed


async def test_pre_cancelled_result_does_not_open_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    value = store()
    monkeypatch.setattr(value, "_new_client", lambda: pytest.fail("cancelled I/O"))
    with pytest.raises(TtsResultError, match="tts_result_cancelled"):
        await value.stage(prepared(), cancelled=lambda: True)


@pytest.mark.parametrize("first_error", [None, TtsResultError("tts_result_storage_unknown")])
async def test_staging_outcome_never_allows_the_same_synthesis_to_dispatch_twice(
    first_error: Exception | None,
) -> None:
    synthesizer = FakeSynthesizer()
    synthesis = FileTtsSynthesis(
        synthesizer=synthesizer,
        encoder=LocalTtsEncoder(),
        request=TtsRequest("request-1", "Synthetic."),
        policy=POLICY,
        delivery_format=TtsDeliveryFormat.WAV,
        max_payload_bytes=1024 * 1024,
        cancelled=lambda: False,
        execution_id="execution-1",
    )

    class Results:
        calls = 0

        async def stage(self, result: PreparedTtsResult, *, cancelled: Any) -> Any:
            self.calls += 1
            if first_error is not None:
                raise first_error
            return result.receipt("sn-audio-test", "v1")

    results = Results()
    if first_error is None:
        await synthesis.stage_result(
            workspace_id="workspace-one", request_digest="sha256:" + "a" * 64, store=results
        )
    else:
        with pytest.raises(TtsResultError):
            await synthesis.stage_result(
                workspace_id="workspace-one", request_digest="sha256:" + "a" * 64, store=results
            )
    with pytest.raises(ValueError, match="again"):
        await synthesis.stage_result(
            workspace_id="workspace-one", request_digest="sha256:" + "a" * 64, store=results
        )
    assert len(synthesizer.calls) == 1 and results.calls == 1


def test_prepared_audio_rejects_container_or_source_clock_relabeling() -> None:
    result = prepared(TtsDeliveryFormat.PCM)
    with pytest.raises(TtsResultError):
        replace(result, body=result.body + b"\x00\x00")
    with pytest.raises(TtsResultError):
        replace(result, media_type="audio/flac")


@pytest.mark.parametrize(
    "changes",
    [
        {"source_pcm_digest": "sha256:" + "b" * 64},
        {"duration_ms": 99},
        {"sample_rate_hz": 8000},
        {"media_type": "audio/pcm"},
        {"format": TtsDeliveryFormat.PCM},
    ],
)
async def test_an_encoder_cannot_change_the_source_clock_digest_or_requested_format(
    changes: dict[str, Any],
) -> None:
    synthesis, engine, _ = build(encoder=FakeEncoder(delivery=replace(DELIVERY, **changes)))
    with pytest.raises(TtsDeliveryError):
        await synthesis.run()
    assert len(engine.calls) == 1


async def test_invalid_staging_identity_or_unsupported_clock_fails_before_engine_io() -> None:
    synthesis, engine, _ = build()
    with pytest.raises(TtsResultError):
        await synthesis.stage_result(
            workspace_id="bad workspace", request_digest="bad", store=store()
        )
    assert not engine.calls
    engine = FakeSynthesizer()
    synthesis = FileTtsSynthesis(
        synthesizer=engine,
        encoder=LocalTtsEncoder(),
        request=TtsRequest("request-1", "Synthetic."),
        policy=replace(POLICY, sample_rate_hz=8000),
        delivery_format=TtsDeliveryFormat.WAV,
        max_payload_bytes=1024 * 1024,
        cancelled=lambda: False,
        execution_id="execution-1",
    )
    with pytest.raises(ValueError, match="24 kHz"):
        await synthesis.stage_result(
            workspace_id="workspace-one", request_digest="sha256:" + "a" * 64, store=store()
        )
    assert not engine.calls


@pytest.mark.parametrize("max_payload", [False, 1023, 67108865])
def test_invalid_delivery_bound_is_rejected_before_synthesis(max_payload: Any) -> None:
    with pytest.raises(ValueError):
        FileTtsSynthesis(
            synthesizer=FakeSynthesizer(),
            encoder=LocalTtsEncoder(),
            request=TtsRequest("request-1", "Synthetic."),
            policy=POLICY,
            delivery_format=TtsDeliveryFormat.WAV,
            max_payload_bytes=max_payload,
            cancelled=lambda: False,
            execution_id="execution-1",
        )


@pytest.mark.parametrize("method", ["PUT", "HEAD", "GET"])
async def test_audio_trickle_headers_obey_one_total_deadline(server: Server, method: str) -> None:
    result = prepared()
    server.slow_key = "/" + "sn-audio-test/" + result.reference("sn-audio-test").object_key
    server.slow_method = method
    value = MinioTtsResults(
        endpoint=f"http://127.0.0.1:{server.server_port}",
        bucket="sn-audio-test",
        access_key="synthetic",
        secret_key="synthetic",
        timeout_seconds=0.08,
    )
    started = time.monotonic()
    with pytest.raises(TtsResultError, match="tts_result_deadline_exceeded"):
        await value.stage(result, cancelled=lambda: False)
    # The server trickles a header every 30 ms for 1.2 s; a per-packet inactivity timeout would
    # run that long, so staying below it proves the total deadline is what stopped the upload.
    # Keep a wide margin above the 80 ms budget for loaded CI schedulers.
    assert time.monotonic() - started < 1.0
    assert server.bodies[server.slow_key] == result.body
    assert sum(verb == "PUT" for verb, _ in server.calls) == 1


async def test_audio_cancel_joins_its_own_storage_operation(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = prepared()
    server.slow_key = "/sn-audio-test/" + result.reference("sn-audio-test").object_key
    value = MinioTtsResults(
        endpoint=f"http://127.0.0.1:{server.server_port}",
        bucket="sn-audio-test",
        access_key="synthetic",
        secret_key="synthetic",
        timeout_seconds=5,
    )
    factory = value._new_client
    clients = []

    def client() -> httpx.AsyncClient:
        owned = factory()
        clients.append(owned)
        return owned

    monkeypatch.setattr(value, "_new_client", client)
    task = asyncio.create_task(value.stage(result, cancelled=lambda: False))
    assert await asyncio.to_thread(server.started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients and all(client.is_closed for client in clients)
    assert sum(verb == "PUT" for verb, _ in server.calls) == 1
