"""Public speech resolves published facts, tightens the ceiling and never truncates."""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.speech_synthesis import (
    BindingFact,
    SpeechDeadlineExceededError,
    SpeechEngineError,
    SpeechLimits,
    SpeechOutputLimitExceededError,
    SpeechSynthesis,
    SpeechUnavailableError,
    WorkspaceFact,
)
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationUnavailableError,
)
from speechnexus_backend.speech.domain.speech_synthesis import (
    MAX_SPEECH_INSTRUCTIONS_CHARS,
    MAX_SPEECH_TEXT_CHARS,
    SpeechAudio,
    SpeechCommand,
    SpeechFormat,
    SpeechPlan,
)
from speechnexus_backend.speech.interfaces.http import speech as surface
from tests.unit.backend.test_health import _settings

BINDING = Publication("binding", "tts-quality", 2, "binding-rev-2", 1_000)
VOICE = Publication("voice", "voice-vivian", 4, "voice-rev-4", 1_000)
OLD_VOICE = Publication("voice", "voice-vivian", 3, "voice-rev-3", 900)
PROFILE = Publication("profile", "speech-interactive", 1, "profile-rev-1", 1_000)
DEFINITION: dict[str, Any] = {
    "binding_id": "tts-quality",
    "logical_model": "qwen3-tts-customvoice",
    "tasks": ["tts_file"],
    "parameter_domains": {
        "tts": {
            "languages": ["zh", "en"],
            "audio_formats": ["mp3", "wav", "pcm"],
            "sample_rates_hz": [24000],
            "text_commit_modes": ["explicit"],
            "audio_delivery_modes": ["complete"],
            "supports_instructions": True,
            "supports_speed": True,
            "speed_range": {"minimum": 0.5, "maximum": 2.0},
            "max_input_codepoints": 4096,
            "supports_seed": True,
        }
    },
}
REQUEST = {
    "model": "tts-quality",
    "input": "欢迎使用 SpeechNexus。",
    "voice": "voice-vivian",
}


class Facts:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.definition: dict[str, Any] = json.loads(json.dumps(DEFINITION))
        self.published = {
            ("voice", VOICE.object_id, VOICE.revision): VOICE,
            ("voice", OLD_VOICE.object_id, OLD_VOICE.revision): OLD_VOICE,
            ("profile", PROFILE.object_id, PROFILE.revision): PROFILE,
        }

    def workspace(self, workspace_id: str) -> WorkspaceFact | None:
        if self.error is not None:
            raise self.error
        return WorkspaceFact(7, True) if workspace_id == "workspace-default" else None

    def latest_binding(self, model_id: str) -> BindingFact | None:
        if model_id != "tts-quality":
            return None
        return BindingFact(BINDING, self.definition)

    def latest_publication(self, kind: str, object_id: str) -> Publication | None:
        candidates = [
            publication
            for key, publication in self.published.items()
            if key[0] == kind and key[1] == object_id
        ]
        return max(candidates, key=lambda item: item.revision, default=None)

    def publication(self, kind: str, object_id: str, revision: int) -> Publication | None:
        return self.published.get((kind, object_id, revision))

    def pronunciation_exists(self, object_id: str, revision: int) -> bool:
        return False


class Synthesizer:
    def __init__(self, audio: SpeechAudio | None = None, error: Exception | None = None):
        self.audio = audio
        self.error = error
        self.plans: list[SpeechPlan] = []

    def synthesize(self, plan: SpeechPlan) -> SpeechAudio:
        self.plans.append(plan)
        if self.error is not None:
            raise self.error
        assert self.audio is not None
        return self.audio


def pcm(duration_ms: int = 200, rate: int = 24000) -> SpeechAudio:
    frames = rate * duration_ms // 1000
    return SpeechAudio(b"\x00\x00" * frames, SpeechFormat.PCM, rate, 1, duration_ms)


def wav(duration_ms: int = 200, rate: int = 24000) -> SpeechAudio:
    frames = rate * duration_ms // 1000
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * frames)
    return SpeechAudio(buffer.getvalue(), SpeechFormat.WAV, rate, 1, duration_ms)


def mp3() -> SpeechAudio:
    return SpeechAudio(b"\xff\xfb\x90\x64" + bytes(900), SpeechFormat.MP3, 24000, 1, 24)


def client(service: SpeechSynthesis | None) -> AsyncClient:
    app = create_app(settings=_settings(), speech=service)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def service(
    *,
    synth: Synthesizer | None = None,
    facts: Facts | None = None,
    limits: SpeechLimits | None = None,
) -> tuple[SpeechSynthesis, Synthesizer]:
    resolved = synth or Synthesizer(mp3())
    return SpeechSynthesis(facts or Facts(), resolved, limits), resolved


def command(**patch: Any) -> SpeechCommand:
    values: dict[str, Any] = {
        "model": "tts-quality",
        "text": "hi",
        "voice": "voice-vivian",
        "response_format": SpeechFormat.PCM,
    }
    values.update(patch)
    return SpeechCommand(**values)


async def test_success_returns_complete_audio_with_declared_shape() -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json=REQUEST)

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-audio-format"] == "mp3"
    assert response.headers["x-sample-rate-hz"] == "24000"
    assert response.headers["x-channels"] == "1"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert response.content == mp3().payload
    assert len(synth.plans) == 1
    plan = synth.plans[0]
    assert plan.voice_id == "voice-vivian"
    assert plan.voice_revision_id == VOICE.revision_id
    assert plan.response_format is SpeechFormat.MP3
    assert plan.sample_rate_hz == 24000
    assert plan.channels == 1


@pytest.mark.parametrize("name", list(surface.RESPONSE_FORMATS))
async def test_each_declared_format_maps_to_its_media_type(name: str) -> None:
    audio = {"mp3": mp3, "wav": wav, "pcm": pcm}[name]()
    chain, synth = service(synth=Synthesizer(audio))
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json={**REQUEST, "response_format": name})

    assert response.status_code == 200
    assert response.headers["content-type"] == audio.response_format.media_type
    assert response.headers["x-audio-format"] == name
    assert synth.plans[0].response_format is audio.response_format


async def test_pinned_voice_revision_is_used_instead_of_latest() -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech", json={**REQUEST, "nexus": {"voice_revision": "3"}}
        )

    assert response.status_code == 200
    assert synth.plans[0].voice_revision_id == OLD_VOICE.revision_id


async def test_unpublished_model_is_not_found_and_never_defaults() -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json={**REQUEST, "model": "tts-unpublished"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert response.json()["error"]["retryable"] is False
    assert synth.plans == []


async def test_unpublished_voice_is_not_found() -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json={**REQUEST, "voice": "voice-unknown"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "voice_not_found"
    assert synth.plans == []


async def test_published_model_without_tts_task_is_unsupported() -> None:
    facts = Facts()
    facts.definition = {**facts.definition, "tasks": ["asr_file"]}
    chain, synth = service(facts=facts)
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json=REQUEST)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert response.json()["error"]["param"] == "model"
    assert synth.plans == []


async def test_publication_storage_failure_is_unavailable_not_500() -> None:
    chain, _ = service(facts=Facts(error=PublicationUnavailableError()))
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json=REQUEST)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backend_unavailable"
    assert response.json()["error"]["retryable"] is True


async def test_unconfigured_speech_backend_is_unavailable() -> None:
    async with client(None) as http:
        response = await http.post("/v1/audio/speech", json=REQUEST)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backend_unavailable"


@pytest.mark.parametrize(
    ("patch", "param"),
    [
        ({"speed": 5.0}, "speed"),
        ({"speed": 0.1}, "speed"),
        ({"input": ""}, "input"),
        ({"input": "x" * (MAX_SPEECH_TEXT_CHARS + 1)}, "input"),
        ({"stream_format": "sse"}, "stream_format"),
        ({"response_format": "flac"}, "response_format"),
        ({"unexpected": True}, "unexpected"),
        ({"instructions": "x" * (MAX_SPEECH_INSTRUCTIONS_CHARS + 1)}, "instructions"),
    ],
)
async def test_contract_violations_are_bad_requests(patch: dict[str, Any], param: str) -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json={**REQUEST, **patch})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["param"] == param
    assert synth.plans == []


async def test_binding_domain_violation_is_unsupported_not_clamped() -> None:
    facts = Facts()
    facts.definition["parameter_domains"]["tts"]["audio_formats"] = ["mp3"]
    chain, synth = service(facts=facts)
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json={**REQUEST, "response_format": "wav"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert response.json()["error"]["param"] == "response_format"
    assert synth.plans == []


async def test_speed_outside_binding_range_is_unsupported() -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json={**REQUEST, "speed": 4.0})

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "speed"
    assert synth.plans == []


async def test_engine_failure_returns_an_error_and_no_audio() -> None:
    chain, _ = service(synth=Synthesizer(error=SpeechEngineError()))
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json=REQUEST)

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["code"] == "backend_unavailable"
    assert "x-audio-format" not in response.headers


async def test_deadline_exhaustion_is_a_gateway_timeout() -> None:
    chain, _ = service(synth=Synthesizer(error=SpeechDeadlineExceededError()))
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json=REQUEST)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "operation_timeout"
    assert response.json()["error"]["retryable"] is True


async def test_one_hundred_millisecond_request_cannot_return_two_hundred() -> None:
    chain, synth = service(
        synth=Synthesizer(pcm(duration_ms=200)),
        limits=SpeechLimits(max_output_audio_ms=100),
    )
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json={**REQUEST, "response_format": "pcm"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "speech_output_limit_exceeded"
    assert not response.content.startswith(b"RIFF")
    plan = synth.plans[0]
    assert plan.max_audio_ms == 100
    assert plan.max_source_pcm_bytes == 4800
    assert plan.max_payload_bytes == 4800


def test_request_ceiling_tightens_the_synthesizer_policy() -> None:
    chain, synth = service(
        synth=Synthesizer(pcm(duration_ms=100)),
        limits=SpeechLimits(max_output_audio_ms=600_000),
    )

    chain.synthesize(command(), request_id="req:tighten", max_output_audio_ms=100)

    plan = synth.plans[0]
    assert plan.max_audio_ms == 100
    assert plan.max_payload_bytes == 4800


def test_request_ceiling_never_exceeds_the_deployment_ceiling() -> None:
    chain, synth = service(
        synth=Synthesizer(pcm(duration_ms=3000)),
        limits=SpeechLimits(max_output_audio_ms=2_000),
    )

    with pytest.raises(SpeechOutputLimitExceededError):
        chain.synthesize(command(), request_id="req:clamped", max_output_audio_ms=60_000)
    assert synth.plans[0].max_audio_ms == 2_000


def test_wav_payload_without_a_header_is_rejected() -> None:
    with pytest.raises(ValueError):
        SpeechAudio(b"not-a-wave", SpeechFormat.WAV, 24000, 1, 100)


async def test_method_not_allowed_uses_the_speech_envelope() -> None:
    chain, _ = service()
    async with client(chain) as http:
        response = await http.get("/v1/audio/speech")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "speech_method_not_allowed"
    assert "allow" in response.headers


async def test_idempotency_key_is_refused_instead_of_ignored() -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech", json=REQUEST, headers={"Idempotency-Key": "speech-key-1"}
        )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "Idempotency-Key"
    assert synth.plans == []


async def test_disabled_workspace_is_refused() -> None:
    class Inactive(Facts):
        def workspace(self, workspace_id: str) -> WorkspaceFact | None:
            return WorkspaceFact(7, False)

    chain, synth = service(facts=Inactive())
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json=REQUEST)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workspace_disabled"
    assert synth.plans == []


async def test_unavailable_synthesizer_is_reported_as_unavailable() -> None:
    chain, _ = service(synth=Synthesizer(error=SpeechUnavailableError()))
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json=REQUEST)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backend_unavailable"


def test_surface_constants_match_the_frozen_contract() -> None:
    source = Path(__file__).resolve().parents[3] / "contracts" / "speech-openapi.yaml"
    document = yaml.safe_load(source.read_text())
    schemas = document["components"]["schemas"]
    request = schemas["SpeechRequest"]
    assert request["additionalProperties"] is False
    assert set(request["required"]) == {"model", "input", "voice"}
    assert set(request["properties"]) == surface.TOP_LEVEL_FIELDS
    assert request["properties"]["input"]["maxLength"] == MAX_SPEECH_TEXT_CHARS
    assert request["properties"]["instructions"]["maxLength"] == MAX_SPEECH_INSTRUCTIONS_CHARS
    assert request["properties"]["speed"]["minimum"] == surface.SPEED_MIN
    assert request["properties"]["speed"]["maximum"] == surface.SPEED_MAX
    assert request["properties"]["stream_format"]["enum"] == list(surface.STREAM_FORMATS)
    formats = yaml.safe_load(
        (source.parent / "shared" / "capability-domains.schema.json").read_text()
    )["$defs"]["AudioFormat"]["enum"]
    assert tuple(formats) == surface.RESPONSE_FORMATS

    nexus = schemas["SpeechNexusOptions"]
    assert nexus["additionalProperties"] is False
    assert set(nexus["properties"]) == surface.NEXUS_FIELDS
    assert nexus["dependentRequired"] == {
        "profile_id": ["profile_revision"],
        "profile_revision": ["profile_id"],
    }
    assert nexus["properties"]["deadline_ms"]["maximum"] == surface.DEADLINE_MAX_MS

    overrides = schemas["SpeechOverrides"]
    assert set(overrides["properties"]) == surface.OVERRIDE_FIELDS
    tts = schemas["RestTtsOverrides"]
    assert tts["additionalProperties"] is False
    assert set(tts["properties"]) == surface.TTS_OVERRIDE_FIELDS
    planner = tts["properties"]["planner"]["properties"]
    assert set(planner) == surface.PLANNER_FIELDS
    assert planner["max_chars"]["minimum"] == surface.PLANNER_CHARS_MIN
    assert planner["max_chars"]["maximum"] == surface.PLANNER_CHARS_MAX
    assert planner["max_wait_ms"]["minimum"] == surface.PLANNER_WAIT_MIN
    assert planner["max_wait_ms"]["maximum"] == surface.PLANNER_WAIT_MAX
    assert tts["properties"]["audio_delivery"]["const"] == "complete"


@pytest.mark.parametrize(
    "trace",
    ["AB", "ABCDEF-0123", "urn:UUID:ABC", "550E8400-E29B-41D4-A716-446655440000", "a" * 128],
)
async def test_platform_correlation_ids_never_fail_a_valid_request(trace: str) -> None:
    """An X-Request-ID the HTTP layer accepted must never become a 503.

    The middleware accepts wider values (uppercase hex, short trace IDs) than the
    domain identifier grammar. Blaming the synthesizer for that disagreement made
    every client echoing an uppercase UUID fail 100% of the time.
    """
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech", json=REQUEST, headers={"X-Request-ID": trace}
        )

    assert response.status_code == 200
    assert response.content == mp3().payload
    assert len(synth.plans) == 1
    plan_request_id = synth.plans[0].request_id
    assert plan_request_id.islower()
    assert len(plan_request_id) >= 3


async def test_valid_lowercase_correlation_id_is_preserved_verbatim() -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech", json=REQUEST, headers={"X-Request-ID": "trace-0123abcd"}
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "trace-0123abcd"
    assert synth.plans[0].request_id == "trace-0123abcd"
