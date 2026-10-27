"""Public transcription resolves published facts, bounds media and never truncates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.transcription import (
    BindingFact,
    Transcription,
    TranscriptionAudioTooLongError,
    TranscriptionAudioUnsupportedError,
    TranscriptionEngineError,
    TranscriptionLimits,
    TranscriptionMediaTooLargeError,
    TranscriptionUnavailableError,
    WorkspaceFact,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.domain.transcription import (
    TIMESTAMP_GRANULARITIES,
    AudioFacts,
    TranscriptionCommand,
    TranscriptionPlan,
    TranscriptionResult,
    TranscriptSegment,
    TranscriptWord,
)
from speechnexus_backend.speech.interfaces.http import transcriptions as surface
from tests.unit.backend.test_health import _settings

BINDING = Publication("binding", "asr-quality", 3, "binding-rev-3", 1_000)
PROFILE = Publication("profile", "file-accurate", 1, "profile-rev-1", 1_000)
DEFINITION: dict[str, Any] = {
    "binding_id": "asr-quality",
    "logical_model": "qwen3-asr-1.7b",
    "tasks": ["asr_file", "asr_full_segment_review"],
    "parameter_domains": {
        "asr": {
            "languages": ["zh", "en", "auto"],
            "max_segment_ms": 30_000,
            "finalization_modes": ["full_segment"],
            "supports_prompt": True,
            "supports_word_timestamps": True,
            "alignment_modes": ["off"],
            "diarization_modes": ["off"],
        }
    },
}
AUDIO = b"\x00\x01" * 4096
FACTS = AudioFacts("audio/wav", "pcm_s16le", 1_500, 16_000, 1)
FIELDS = {"model": "asr-quality"}


class Facts:
    def __init__(self, *, definition: dict[str, Any] | None = None) -> None:
        self.definition: dict[str, Any] = json.loads(json.dumps(definition or DEFINITION))
        self.published = {("profile", PROFILE.object_id, PROFILE.revision): PROFILE}

    def workspace(self, workspace_id: str) -> WorkspaceFact | None:
        return WorkspaceFact(7, True) if workspace_id == "workspace-default" else None

    def latest_binding(self, model_id: str) -> BindingFact | None:
        if model_id != "asr-quality":
            return None
        return BindingFact(BINDING, self.definition)

    def publication(self, kind: str, object_id: str, revision: int) -> Publication | None:
        return self.published.get((kind, object_id, revision))


class Probe:
    def __init__(self, facts: AudioFacts | None = None, error: Exception | None = None) -> None:
        self.facts = facts or FACTS
        self.error = error
        self.calls: list[tuple[bytes, int, int]] = []

    def inspect(self, audio: bytes, *, max_audio_ms: int, max_upload_bytes: int) -> AudioFacts:
        self.calls.append((audio, max_audio_ms, max_upload_bytes))
        if self.error is not None:
            raise self.error
        return self.facts


class Transcriber:
    def __init__(
        self, result: TranscriptionResult | None = None, error: Exception | None = None
    ) -> None:
        self.result = result or TranscriptionResult("hello", "en", 1_500)
        self.error = error
        self.plans: list[TranscriptionPlan] = []

    def transcribe(self, plan: TranscriptionPlan) -> TranscriptionResult:
        self.plans.append(plan)
        if self.error is not None:
            raise self.error
        return self.result


def chain(
    *,
    facts: Facts | None = None,
    probe: Probe | None = None,
    transcriber: Transcriber | None = None,
    limits: TranscriptionLimits | None = None,
) -> tuple[Transcription, Transcriber, Probe]:
    resolved_probe = probe or Probe()
    resolved_transcriber = transcriber or Transcriber()
    service = Transcription(facts or Facts(), resolved_probe, resolved_transcriber, limits)
    return service, resolved_transcriber, resolved_probe


def client(service: Transcription | None) -> AsyncClient:
    app = create_app(settings=_settings(), transcriptions=service)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def files(audio: bytes = AUDIO) -> dict[str, Any]:
    return {"file": ("sample.wav", audio, "application/octet-stream")}


async def test_success_returns_json_and_threads_the_media_ceiling() -> None:
    service, transcriber, probe = chain(limits=TranscriptionLimits(max_audio_ms=2_000))
    async with client(service) as http:
        response = await http.post("/v1/audio/transcriptions", data=FIELDS, files=files())

    assert response.status_code == 200
    assert response.json()["text"] == "hello"
    assert response.json()["usage"] == {"type": "duration", "seconds": 1.5}
    plan = transcriber.plans[0]
    assert plan.max_audio_ms == 2_000
    assert plan.max_segment_ms == 30_000
    assert plan.language == "auto"
    assert plan.audio == AUDIO
    assert probe.calls == [(AUDIO, 2_000, service.limits.max_upload_bytes)]


async def test_text_format_returns_plain_text() -> None:
    service, _, _ = chain()
    async with client(service) as http:
        response = await http.post(
            "/v1/audio/transcriptions",
            data=FIELDS | {"response_format": "text"},
            files=files(),
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "hello"


async def test_verbose_json_carries_real_segment_and_word_evidence() -> None:
    result = TranscriptionResult(
        "hello world",
        "en",
        1_500,
        segments=(TranscriptSegment(0, 0, 1_500, "hello world", (1, 2), -0.1, 1.2, 0.01, 0, 0.0),),
        words=(TranscriptWord("hello", 0, 700), TranscriptWord("world", 800, 1_500)),
    )
    service, _, _ = chain(transcriber=Transcriber(result))
    async with client(service) as http:
        response = await http.post(
            "/v1/audio/transcriptions",
            data=FIELDS
            | {
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["segment", "word"],
            },
            files=files(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["duration"] == 1.5
    assert body["language"] == "en"
    assert body["segments"][0]["text"] == "hello world"
    assert body["segments"][0]["end"] == 1.5
    assert body["words"][1]["word"] == "world"


async def test_srt_is_rendered_from_real_segments() -> None:
    result = TranscriptionResult(
        "hello",
        "en",
        1_500,
        segments=(TranscriptSegment(0, 0, 1_500, "hello", (1,), -0.1, 1.0, 0.0, 0, 0.0),),
    )
    service, _, _ = chain(transcriber=Transcriber(result))
    async with client(service) as http:
        response = await http.post(
            "/v1/audio/transcriptions",
            data=FIELDS | {"response_format": "srt"},
            files=files(),
        )

    assert response.status_code == 200
    assert response.text.startswith("1\n00:00:00,000 --> 00:00:01,500\nhello")


async def test_verbose_json_without_segment_evidence_is_unsupported() -> None:
    service, _, _ = chain()
    async with client(service) as http:
        response = await http.post(
            "/v1/audio/transcriptions",
            data=FIELDS | {"response_format": "verbose_json"},
            files=files(),
        )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unsupported_parameter"
    assert error["param"] == "response_format"


async def test_unpublished_model_is_not_found() -> None:
    service, transcriber, probe = chain()
    async with client(service) as http:
        response = await http.post(
            "/v1/audio/transcriptions",
            data={"model": "asr-unpublished"},
            files=files(),
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert transcriber.plans == []
    assert probe.calls == []


async def test_absent_service_is_unavailable() -> None:
    async with client(None) as http:
        response = await http.post("/v1/audio/transcriptions", data=FIELDS, files=files())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backend_unavailable"


async def test_engine_failure_returns_no_partial_text() -> None:
    service, _, _ = chain(transcriber=Transcriber(error=TranscriptionEngineError()))
    async with client(service) as http:
        response = await http.post("/v1/audio/transcriptions", data=FIELDS, files=files())

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "backend_unavailable"
    assert "text" not in response.json()


async def test_unavailable_backend_is_reported() -> None:
    service, _, _ = chain(transcriber=Transcriber(error=TranscriptionUnavailableError()))
    async with client(service) as http:
        response = await http.post("/v1/audio/transcriptions", data=FIELDS, files=files())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backend_unavailable"


@pytest.mark.parametrize(
    "changes,param",
    [
        ({"language": "xx"}, "language"),
        ({"prompt": "term"}, "prompt"),
        (
            {"response_format": "verbose_json", "timestamp_granularities[]": ["word"]},
            "timestamp_granularities[]",
        ),
        (
            {"nexus": json.dumps({"overrides": {"asr": {"turn_detection": {"mode": "server"}}}})},
            "nexus.overrides.asr.turn_detection",
        ),
    ],
)
async def test_binding_limits_are_enforced(changes: dict[str, Any], param: str) -> None:
    definition = json.loads(json.dumps(DEFINITION))
    definition["parameter_domains"]["asr"]["supports_prompt"] = False
    definition["parameter_domains"]["asr"]["supports_word_timestamps"] = False
    service, transcriber, _ = chain(facts=Facts(definition=definition))
    async with client(service) as http:
        response = await http.post("/v1/audio/transcriptions", data=FIELDS | changes, files=files())

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unsupported_parameter"
    assert error["param"] == param
    assert transcriber.plans == []


async def test_workspace_and_profile_absence_is_explicit() -> None:
    service, _, _ = chain()
    async with client(service) as http:
        missing_workspace = await http.post(
            "/v1/audio/transcriptions",
            data=FIELDS | {"nexus": json.dumps({"workspace_id": "workspace-other"})},
            files=files(),
        )
        missing_profile = await http.post(
            "/v1/audio/transcriptions",
            data=FIELDS
            | {"nexus": json.dumps({"profile_id": "file-accurate", "profile_revision": "9"})},
            files=files(),
        )

    assert missing_workspace.json()["error"]["code"] == "workspace_not_found"
    assert missing_profile.status_code == 422
    assert missing_profile.json()["error"]["code"] == "profile_not_published"


@pytest.mark.parametrize(
    "changes",
    [
        {"model": ""},
        {"model": "asr-quality", "response_format": "xml"},
        {"model": "asr-quality", "timestamp_granularities[]": ["sentence"]},
        {"model": "asr-quality", "timestamp_granularities[]": ["segment"]},
        {"model": "asr-quality", "prompt": "x" * 4001},
        {"model": "asr-quality", "language": "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"},
        {"model": "asr-quality", "unexpected": "1"},
        {"model": "asr-quality", "nexus": "{}", "nexus_extra": "1"},
        {"model": "asr-quality", "nexus": "{not json"},
        {"model": "asr-quality", "nexus": json.dumps({"profile_id": "file-accurate"})},
        {"model": "asr-quality", "nexus": json.dumps({"deadline_ms": 0})},
        {"model": "asr-quality", "nexus": json.dumps({"workspace_id": "../etc"})},
    ],
)
async def test_contract_violations_are_bad_requests(changes: dict[str, str]) -> None:
    service, transcriber, probe = chain()
    async with client(service) as http:
        response = await http.post("/v1/audio/transcriptions", data=changes, files=files())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert transcriber.plans == []
    assert probe.calls == []


async def test_unsupported_audio_format_is_a_bad_request_without_internals() -> None:
    probe = Probe(error=TranscriptionAudioUnsupportedError())
    service, transcriber, _ = chain(probe=probe)
    async with client(service) as http:
        response = await http.post(
            "/v1/audio/transcriptions", data=FIELDS, files=files(b"not audio" * 200)
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["param"] == "file"
    assert transcriber.plans == []
    body = response.text
    for leak in ("ffmpeg", "/tmp", "http://", "Unknown input format"):
        assert leak not in body


async def test_oversized_upload_is_payload_too_large() -> None:
    probe = Probe(error=TranscriptionMediaTooLargeError())
    service, transcriber, _ = chain(probe=probe)
    async with client(service) as http:
        response = await http.post("/v1/audio/transcriptions", data=FIELDS, files=files())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert transcriber.plans == []


async def test_audio_over_the_sync_budget_requires_a_job() -> None:
    probe = Probe(error=TranscriptionAudioTooLongError())
    service, transcriber, _ = chain(probe=probe)
    async with client(service) as http:
        response = await http.post("/v1/audio/transcriptions", data=FIELDS, files=files())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "async_required"
    assert transcriber.plans == []


async def test_result_over_the_ceiling_fails_the_whole_request() -> None:
    result = TranscriptionResult("hello", "en", 5_000)
    service, _, _ = chain(
        transcriber=Transcriber(result), limits=TranscriptionLimits(max_audio_ms=2_000)
    )
    async with client(service) as http:
        response = await http.post("/v1/audio/transcriptions", data=FIELDS, files=files())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "transcription_output_limit_exceeded"


async def test_segment_override_tightens_the_plan_ceiling() -> None:
    service, transcriber, _ = chain()
    async with client(service) as http:
        response = await http.post(
            "/v1/audio/transcriptions",
            data=FIELDS | {"nexus": json.dumps({"overrides": {"asr": {"max_segment_ms": 1000}}})},
            files=files(),
        )

    assert response.status_code == 200
    assert transcriber.plans[0].max_segment_ms == 1_000


async def test_missing_file_and_wrong_media_type_are_rejected() -> None:
    service, transcriber, _ = chain()
    async with client(service) as http:
        missing = await http.post("/v1/audio/transcriptions", data=FIELDS)
        wrong = await http.post(
            "/v1/audio/transcriptions",
            data=FIELDS,
            files=files(),
            headers={"Content-Type": "application/json"},
        )

    assert missing.status_code == 400
    assert missing.json()["error"]["param"] == "file"
    assert wrong.status_code == 400
    assert transcriber.plans == []


async def test_idempotency_key_is_refused_instead_of_ignored() -> None:
    service, transcriber, _ = chain()
    async with client(service) as http:
        response = await http.post(
            "/v1/audio/transcriptions",
            data=FIELDS,
            files=files(),
            headers={"Idempotency-Key": "abc"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "Idempotency-Key"
    assert transcriber.plans == []


def test_surface_constants_match_the_frozen_contract() -> None:
    root = Path(__file__).resolve().parents[3]
    document = yaml.safe_load((root / "contracts" / "speech-openapi.yaml").read_text())
    schemas = document["components"]["schemas"]
    request = schemas["TranscriptionRequest"]
    assert request["additionalProperties"] is False
    assert set(request["required"]) == {"file", "model"}
    assert set(request["properties"]) == surface.TOP_LEVEL_FIELDS
    assert request["properties"]["prompt"]["maxLength"] == 4000
    assert request["properties"]["language"]["maxLength"] == 35
    assert request["properties"]["nexus"]["maxLength"] == surface.MAX_NEXUS_CHARS
    formats = yaml.safe_load(
        (root / "contracts" / "shared" / "capability-domains.schema.json").read_text()
    )["$defs"]["TranscriptFormat"]["enum"]
    assert tuple(formats) == surface.RESPONSE_FORMATS
    assert request["properties"]["timestamp_granularities[]"]["items"]["enum"] == list(
        TIMESTAMP_GRANULARITIES
    )

    nexus = schemas["TranscriptionNexusOptions"]
    assert nexus["additionalProperties"] is False
    assert set(nexus["properties"]) == surface.NEXUS_FIELDS
    assert nexus["properties"]["deadline_ms"]["maximum"] == surface.DEADLINE_MAX_MS
    overrides = schemas["TranscriptionOverrides"]
    assert set(overrides["properties"]) == surface.OVERRIDE_FIELDS


def test_request_ceilings_only_tighten_the_deployment_policy() -> None:
    service, transcriber, probe = chain(
        limits=TranscriptionLimits(max_audio_ms=2_000, max_upload_bytes=8_192)
    )

    small = b"\x00" * 1_024
    command = TranscriptionCommand(
        model="asr-quality",
        audio=small,
        language="en",
        max_audio_ms=5_000,
        max_upload_bytes=1_024,
    )
    result = service.transcribe(command, request_id="req-tighten")

    assert result.text == "hello"
    plan = transcriber.plans[0]
    assert plan.max_audio_ms == 2_000
    assert plan.max_upload_bytes == 1_024
    assert probe.calls == [(small, 2_000, 1_024)]


async def test_body_ingress_is_bounded_before_multipart_parsing() -> None:
    from starlette.requests import Request

    async def receive() -> dict[str, object] | None:
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/audio/transcriptions",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=xyz"),
            (b"content-length", b"70000"),
        ],
        "state": {"request_id": "req-ingress"},
    }

    with pytest.raises(surface._PayloadTooLargeError):
        await surface._receive(Request(scope, receive), 65536, 5)
