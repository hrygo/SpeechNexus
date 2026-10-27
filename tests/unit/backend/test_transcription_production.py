"""Production synchronous ASR reaches the one gateway client and never guesses."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
import rfc8785
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.transcription import (
    AdmittedExecution,
    StagedAudio,
    Transcription,
    TranscriptionLimits,
    TranscriptionOutcomeUnknownError,
)
from speechnexus_backend.speech.domain.transcription import (
    TranscriptFormat,
    TranscriptionCommand,
    TranscriptionPlan,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.gateway_transcription import (
    GatewayTranscriber,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import AsrParameters
from speechnexus_backend.speech.infrastructure.transcription_submit import (
    TranscriptionCommandPlanner,
)
from speechnexus_backend.transcription_runtime import build_transcription
from tests.unit.backend.test_gateway_execution_client import (
    DIGEST,
    TOKEN,
    status_response,
    submit_response,
)
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_transcription_http import AUDIO, FACTS, FIELDS, Facts, Probe

_IDENTITY_FIELDS = (
    "submit_intent_id",
    "lease_id",
    "operation_id",
    "attempt_id",
    "stage_id",
    "workspace_id",
    "pool_id",
    "host_node_id",
    "gateway_id",
    "instance_id",
    "instance_generation",
    "binding_revision_id",
    "policy_revision_id",
    "voice_revision_id",
    "fencing_token",
    "command_digest",
    "deadline_at",
    "sample_rate_hz",
)


class Inputs:
    def __init__(self) -> None:
        self.plans: list[TranscriptionPlan] = []

    def put(self, plan: TranscriptionPlan) -> StagedAudio:
        self.plans.append(plan)
        return StagedAudio(
            "transcription-input-test",
            plan.workspace_id,
            "inputs",
            "private/transcription-inputs/test.audio",
            "sha256:" + hashlib.sha256(plan.audio).hexdigest(),
            len(plan.audio),
        )


class Admission:
    def admit(
        self, plan: TranscriptionPlan, *, submit_intent_id: str, audio: StagedAudio
    ) -> AdmittedExecution:
        assert audio.content_sha256 == "sha256:" + hashlib.sha256(plan.audio).hexdigest()
        return AdmittedExecution(
            submit_intent_id=submit_intent_id,
            lease_id="lease-test",
            operation_id="operation-test",
            attempt_id="attempt-test",
            stage_id="stage-test",
            workspace_id=plan.workspace_id,
            pool_id="asr-batch",
            host_node_id="host-test",
            gateway_id="gateway-test",
            instance_id="instance-test",
            instance_generation=1,
            binding_revision_id="binding-test",
            policy_revision_id="policy-test",
            fencing_token=7,
            deadline_at_us=4_000_000_000_000_000,
        )


class Results:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.reads: list[tuple[dict[str, Any], str]] = []

    def read_json(self, reference: dict[str, Any], storage_version: str) -> dict[str, Any]:
        self.reads.append((reference, storage_version))
        return dict(self.document)


def gateway(handler: Any) -> GatewayExecutionClient:
    return GatewayExecutionClient(
        base_url="http://gateway.test",
        credential=SecretStr(TOKEN),
        timeout_seconds=1,
        max_retries=0,
        backoff_seconds=0,
        _transport=httpx.MockTransport(handler),
    )


def planner() -> TranscriptionCommandPlanner:
    return TranscriptionCommandPlanner(AsrParameters(400, 1_000, 0.0, 5_000, 1_000))


def handler_for(calls: list[httpx.Request], document: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/submit"):
            sent = json.loads(request.content)
            response = submit_response()
            for field in _IDENTITY_FIELDS:
                if field in sent:
                    response[field] = sent[field]
            response["execution_state"] = "executing"
            response["cleanup_state"] = "pending"
            return httpx.Response(200, json=response)
        if path.endswith("/staging-result"):
            reference = {
                "asset_or_artifact_id": "execution-result-test",
                "workspace_id": "workspace-default",
                "storage_bucket": "results",
                "object_key": "private/execution-results/test/transcription.json",
                "content_sha256": DIGEST,
                "byte_length": 128,
            }
            return httpx.Response(
                200,
                json={
                    "submit_intent_id": path.split("/")[-2],
                    "request_digest": DIGEST,
                    "staging_result": {
                        "request_digest": DIGEST,
                        "reference": reference,
                        "storage_version": "version-test",
                        "media_type": "application/json",
                    },
                },
            )
        sent = calls[0]
        submitted = json.loads(sent.content)
        response = status_response()
        for field in _IDENTITY_FIELDS:
            if field in submitted:
                response[field] = submitted[field]
        response["execution_state"] = "succeeded"
        response["cleanup_state"] = "complete"
        response["stop_evidence_kind"] = "engine_terminal_response"
        return httpx.Response(200, json=response)

    return handler


def service_for(
    calls: list[httpx.Request], document: dict[str, Any]
) -> tuple[Transcription, GatewayTranscriber]:
    results = Results(document)
    transcriber = GatewayTranscriber(
        gateway(handler_for(calls, document)),
        Admission(),
        Inputs(),
        results,
        planner(),
    )
    return (
        Transcription(Facts(), Probe(), transcriber, TranscriptionLimits(max_audio_ms=5_000)),
        transcriber,
    )


async def test_default_route_uses_the_production_gateway_transcriber() -> None:
    calls: list[httpx.Request] = []
    service, transcriber = service_for(calls, {"text": "gateway text", "duration_ms": 1_500})
    try:
        app = create_app(settings=_settings(), transcriptions=service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.post(
                "/v1/audio/transcriptions",
                data=FIELDS,
                files={"file": ("sample.wav", AUDIO, "application/octet-stream")},
            )
    finally:
        transcriber.close()

    assert response.status_code == 200
    assert response.json() == {
        "text": "gateway text",
        "usage": {"type": "duration", "seconds": 1.5},
    }
    assert [request.url.path for request in calls] == [
        "/internal/v1/executions/submit",
        "/internal/v1/executions/submit-"
        + hashlib.sha256(
            ("synchronous-transcription:" + response.headers["x-request-id"]).encode()
        ).hexdigest()[:48],
        "/internal/v1/executions/submit-"
        + hashlib.sha256(
            ("synchronous-transcription:" + response.headers["x-request-id"]).encode()
        ).hexdigest()[:48]
        + "/staging-result",
    ]
    command = json.loads(calls[0].content)
    assert command["command"]["kind"] == "asr"
    assert command["command_digest"] == (
        "sha256:" + hashlib.sha256(rfc8785.dumps(command["command"])).hexdigest()
    )
    assert command["command"]["audio"]["storage"]["content_sha256"] == (
        "sha256:" + hashlib.sha256(AUDIO).hexdigest()
    )
    assert command["command"]["audio"]["sample_rate_hz"] == FACTS.sample_rate_hz


def plan(request_id: str = "request-production") -> TranscriptionPlan:
    return TranscriptionPlan(
        request_id=request_id,
        workspace_id="workspace-default",
        model="asr-quality",
        binding_publication_id="binding-rev-3",
        audio=AUDIO,
        audio_media_type="audio/wav",
        audio_sample_rate_hz=16_000,
        audio_channels=1,
        response_format=TranscriptFormat.JSON,
        language="auto",
        timeout_seconds=0.05,
    )


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
def test_gateway_transport_ambiguity_is_unknown_not_failed(failure: type[Exception]) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise failure("synthetic transport failure", request=request)

    transcriber = GatewayTranscriber(
        gateway(handler), Admission(), Inputs(), Results({"text": "unused"}), planner()
    )
    try:
        with pytest.raises(TranscriptionOutcomeUnknownError):
            transcriber.transcribe(plan())
    finally:
        transcriber.close()
    assert len(calls) == 1


def test_absent_language_and_duration_remain_absent() -> None:
    calls: list[httpx.Request] = []
    service, transcriber = service_for(calls, {"text": "engine text"})
    try:
        result = service.transcribe(
            TranscriptionCommand("asr-quality", AUDIO),
            request_id="request-absent-fields",
        )
    finally:
        transcriber.close()

    assert result.text == "engine text"
    assert result.language is None
    assert result.duration_ms is None


def test_missing_operator_configuration_is_named_and_route_remains_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    assert build_transcription(settings) is None
    message = caplog.text
    for name in (
        "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE",
        "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
        "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
        "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET",
    ):
        assert name in message

    app = create_app(settings=settings, transcriptions=None)
    import asyncio

    async def request() -> httpx.Response:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            return await http.post(
                "/v1/audio/transcriptions",
                data=FIELDS,
                files={"file": ("sample.wav", AUDIO, "application/octet-stream")},
            )

    response = asyncio.run(request())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backend_unavailable"


_FULL_SEGMENT = {
    "id": 0,
    "start_ms": 0,
    "end_ms": 1_000,
    "text": "engine text",
    "tokens": [],
    "avg_logprob": -0.1,
    "compression_ratio": 1.0,
    "no_speech_prob": 0.01,
    "seek": 0,
    "temperature": 0.0,
}


async def test_json_without_engine_duration_omits_usage() -> None:
    calls: list[httpx.Request] = []
    service, transcriber = service_for(calls, {"text": "engine text"})
    try:
        app = create_app(settings=_settings(), transcriptions=service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.post(
                "/v1/audio/transcriptions",
                data=FIELDS,
                files={"file": ("sample.wav", AUDIO, "application/octet-stream")},
            )
    finally:
        transcriber.close()

    assert response.status_code == 200
    assert response.json() == {"text": "engine text"}


async def test_verbose_json_without_duration_or_language_is_not_guessed() -> None:
    calls: list[httpx.Request] = []
    service, transcriber = service_for(calls, {"text": "engine text", "segments": [_FULL_SEGMENT]})
    try:
        app = create_app(settings=_settings(), transcriptions=service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.post(
                "/v1/audio/transcriptions",
                data=FIELDS | {"response_format": "verbose_json"},
                files={"file": ("sample.wav", AUDIO, "application/octet-stream")},
            )
    finally:
        transcriber.close()

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert response.json()["error"]["param"] == "response_format"
