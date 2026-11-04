"""Public speech resolves published facts, tightens the ceiling and never truncates."""

from __future__ import annotations

import io
import json
import wave
from dataclasses import replace
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
    SpeechUnsupportedError,
    WorkspaceFact,
)
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationUnavailableError,
)
from speechnexus_backend.speech.domain.speech_synthesis import (
    MAX_AUDIO_MS,
    MAX_SPEECH_INSTRUCTIONS_CHARS,
    MAX_SPEECH_TEXT_CHARS,
    MIN_AUDIO_MS,
    SpeechAudio,
    SpeechCommand,
    SpeechFormat,
    SpeechPlan,
    identifier,
)
from speechnexus_backend.speech.interfaces.http import speech as surface
from speechnexus_backend.speech.interfaces.http import speech_errors
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


@pytest.mark.parametrize(
    "model_id,workspace_id,binding_revision_id,policy_revision",
    [
        ("tts-quality", "workspace-default", "binding-rev-2", 7),
        ("tts-selected", "workspace-selected", "binding-selected-9", 13),
    ],
)
def test_synthesizer_receives_the_resolved_binding_and_workspace_policy(
    model_id: str, workspace_id: str, binding_revision_id: str, policy_revision: int
) -> None:
    class SelectedFacts(Facts):
        def workspace(self, selected: str) -> WorkspaceFact | None:
            assert selected == workspace_id
            return WorkspaceFact(policy_revision, True)

        def latest_binding(self, selected: str) -> BindingFact | None:
            assert selected == model_id
            return BindingFact(
                Publication("binding", model_id, 9, binding_revision_id, 1_000), self.definition
            )

    chain, synth = service(facts=SelectedFacts(), synth=Synthesizer(pcm()))
    chain.synthesize(command(model=model_id, workspace_id=workspace_id), request_id="request-pin")

    plan = synth.plans[0]
    assert plan.model_id == model_id
    assert plan.workspace_id == workspace_id
    assert plan.binding_revision_id == binding_revision_id
    assert plan.workspace_policy_revision == policy_revision
    assert plan.voice_revision_id == VOICE.revision_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", ""),
        ("model_id", "x" * 129),
        ("workspace_id", "invalid workspace"),
        ("binding_revision_id", "invalid revision"),
        ("workspace_policy_revision", 0),
        ("workspace_policy_revision", True),
        ("workspace_policy_revision", 10_000_000_000_000_000_000),
    ],
)
def test_execution_plan_rejects_invalid_resolved_identity(field: str, value: Any) -> None:
    chain, synth = service(synth=Synthesizer(pcm()))
    chain.synthesize(command(), request_id="request-pin")
    with pytest.raises(ValueError):
        replace(synth.plans[0], **{field: value})


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


@pytest.mark.parametrize(
    "error_type,category",
    [
        (SpeechEngineError, "engine_unavailable"),
        (SpeechUnavailableError, "speech_unavailable"),
        (PublicationUnavailableError, "publication_unavailable"),
    ],
)
async def test_backend_failure_returns_an_error_and_no_audio(
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    category: str,
) -> None:
    private = "synthetic-private-audio-or-credential"
    chain, synth = service(synth=Synthesizer(error=error_type(private)))
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json=REQUEST)

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["code"] == "backend_unavailable"
    assert "x-audio-format" not in response.headers
    records = [record for record in caplog.records if record.name.endswith("http.speech")]
    assert len(records) == 1
    assert synth.plans[0].request_id in records[0].getMessage()
    assert category in records[0].getMessage()
    assert private not in caplog.text and private not in response.text


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
        limits=SpeechLimits(max_output_audio_ms=600_000),
    )
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech",
            json={**REQUEST, "response_format": "pcm", "nexus": {"max_output_audio_ms": 100}},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "speech_output_limit_exceeded"
    assert not response.content.startswith(b"RIFF")
    plan = synth.plans[0]
    assert plan.max_audio_ms == 100
    assert plan.max_source_pcm_bytes == 4800
    assert plan.max_payload_bytes == 4800
    assert len(synth.plans) == 1


def test_request_ceiling_tightens_the_synthesizer_policy() -> None:
    chain, synth = service(
        synth=Synthesizer(pcm(duration_ms=100)),
        limits=SpeechLimits(max_output_audio_ms=600_000),
    )

    chain.synthesize(command(), request_id="req:tighten", max_output_audio_ms=100)

    plan = synth.plans[0]
    assert plan.max_audio_ms == 100
    assert plan.max_payload_bytes == 4800


@pytest.mark.parametrize("name", ["pcm", "wav", "mp3"])
@pytest.mark.parametrize("duration_ms", [100, 200])
async def test_public_ceiling_applies_to_each_delivery(name: str, duration_ms: int) -> None:
    audio = (
        pcm(duration_ms)
        if name == "pcm"
        else wav(duration_ms)
        if name == "wav"
        else replace(mp3(), duration_ms=duration_ms)
    )
    chain, synth = service(synth=Synthesizer(audio))
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech",
            json={**REQUEST, "response_format": name, "nexus": {"max_output_audio_ms": 100}},
        )
    assert response.status_code == (200 if duration_ms == 100 else 503)
    assert len(synth.plans) == 1
    assert synth.plans[0].max_audio_ms == 100
    if duration_ms == 100:
        assert response.content == audio.payload
    else:
        assert response.json()["error"]["code"] == "speech_output_limit_exceeded"
        assert response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("name", ["pcm", "wav", "mp3"])
async def test_public_ceiling_rejects_one_extra_sample_or_encoded_byte(name: str) -> None:
    audio = pcm(100) if name == "pcm" else wav(100) if name == "wav" else mp3()
    # Duration metadata alone would accept this. Check the encoded byte bound too.
    audio = replace(
        audio,
        payload=(audio.payload + b"\x00\x00")
        if name != "mp3"
        else audio.payload[:4] + bytes(9393 - 4),
        duration_ms=100,
    )
    chain, synth = service(synth=Synthesizer(audio))
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech",
            json={**REQUEST, "response_format": name, "nexus": {"max_output_audio_ms": 100}},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "speech_output_limit_exceeded"
    assert len(synth.plans) == 1


@pytest.mark.parametrize("value", [None, True, "100", 99, 3600001, 100.5, {}, []])
async def test_invalid_public_ceiling_never_dispatches(value: Any) -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech", json={**REQUEST, "nexus": {"max_output_audio_ms": value}}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["param"] == "nexus.max_output_audio_ms"
    assert synth.plans == []


async def test_deployment_ceiling_is_stricter_than_the_public_request() -> None:
    chain, synth = service(
        synth=Synthesizer(pcm(200)), limits=SpeechLimits(max_output_audio_ms=100)
    )
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech",
            json={**REQUEST, "response_format": "pcm", "nexus": {"max_output_audio_ms": 200}},
        )
    assert response.status_code == 503
    assert synth.plans[0].max_audio_ms == 100
    assert synth.plans[0].max_source_pcm_bytes == 4800


def test_request_ceiling_never_exceeds_the_deployment_ceiling() -> None:
    chain, synth = service(
        synth=Synthesizer(pcm(duration_ms=3000)),
        limits=SpeechLimits(max_output_audio_ms=2_000),
    )

    with pytest.raises(SpeechOutputLimitExceededError):
        chain.synthesize(command(), request_id="req:clamped", max_output_audio_ms=60_000)
    assert synth.plans[0].max_audio_ms == 2_000


def test_deployment_instruction_limit_rejects_instead_of_truncating() -> None:
    chain, synth = service(limits=SpeechLimits(max_instructions_chars=4))

    with pytest.raises(SpeechUnsupportedError) as raised:
        chain.synthesize(command(instructions="12345"), request_id="req:instructions")

    assert raised.value.param == "instructions"
    assert synth.plans == []


@pytest.mark.parametrize("client_id", ["AB", "ABC", "AB-01", "a" * 129])
async def test_contract_legal_request_id_never_blames_the_synthesizer(client_id: str) -> None:
    """An accepted X-Request-ID must never become a 503.

    The entry point echoes the trace identifier, while execution uses an
    independent server-generated identity.
    """

    chain, synth = service()
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech", json=REQUEST, headers={"X-Request-ID": client_id}
        )

    assert response.status_code == 200
    issued = response.headers["x-request-id"]
    plan_request_id = synth.plans[0].request_id
    assert identifier(plan_request_id) == plan_request_id
    if len(client_id) <= 128:
        # Within the accepted width the client value is echoed verbatim so traces
        # line up; only longer values are replaced by the entry point.
        assert issued == client_id
        assert plan_request_id != client_id
    else:
        assert issued != client_id


async def test_conforming_request_id_is_not_the_execution_identity() -> None:
    chain, synth = service()
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech", json=REQUEST, headers={"X-Request-ID": "req-01:tts"}
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-01:tts"
    assert synth.plans[0].request_id != "req-01:tts"


def test_implemented_response_codes_are_declared_by_the_contract() -> None:
    source = Path(__file__).resolve().parents[3] / "contracts" / "speech-openapi.yaml"
    document = yaml.safe_load(source.read_text())
    declared = {int(code) for code in document["paths"]["/v1/audio/speech"]["post"]["responses"]}
    # 404 is the absent model/voice answer; the error table also carries the
    # method-level 405 the composition root emits for this path.
    implemented = {200, *speech_errors.SPEECH_ERROR_STATUSES}

    assert 404 in declared
    assert implemented <= declared


async def test_tightened_instruction_limit_is_enforced_before_the_engine() -> None:
    chain, synth = service(limits=SpeechLimits(max_instructions_chars=4))
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json={**REQUEST, "instructions": "12345"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert response.json()["error"]["param"] == "instructions"
    assert synth.plans == []


async def test_instructions_within_the_deployment_limit_reach_the_plan() -> None:
    chain, synth = service(limits=SpeechLimits(max_instructions_chars=4))
    async with client(chain) as http:
        response = await http.post("/v1/audio/speech", json={**REQUEST, "instructions": "1234"})

    assert response.status_code == 200
    assert synth.plans[0].instructions == "1234"


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
    assert nexus["properties"]["max_output_audio_ms"]["minimum"] == MIN_AUDIO_MS
    assert nexus["properties"]["max_output_audio_ms"]["maximum"] == MAX_AUDIO_MS

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


async def test_disconnect_records_server_execution_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    from types import SimpleNamespace

    from starlette.requests import ClientDisconnect

    released = threading.Event()
    calls: list[tuple[str, str]] = []
    execution: list[str] = []

    class Service:
        def synthesize(self, command: SpeechCommand, *, request_id: str) -> None:
            execution.append(request_id)
            released.wait(2)

        def cancel(self, request_id: str, reason: str) -> None:
            calls.append((request_id, reason))
            released.set()

    class Request:
        state = SimpleNamespace(request_id="client-trace")
        headers = SimpleNamespace(getlist=lambda name: [])

        async def is_disconnected(self) -> bool:
            await asyncio.sleep(0.05)
            return True

    async def receive(*args: object) -> bytes:
        return json.dumps(REQUEST).encode()

    monkeypatch.setattr(surface, "_receive", receive)
    with pytest.raises(ClientDisconnect):
        await surface._create(Service(), Request(), 65536, 5)  # type: ignore[arg-type]
    assert calls == [(execution[0], "stream_disconnected")]
    assert execution[0] != "client-trace"


@pytest.mark.parametrize("value", [100, 100.0, 3_600_000])
async def test_canonical_integer_ceiling_is_translated_without_relaxing(value: int | float) -> None:
    chain, synth = service(synth=Synthesizer(pcm(100)))
    async with client(chain) as http:
        response = await http.post(
            "/v1/audio/speech",
            json={**REQUEST, "response_format": "pcm", "nexus": {"max_output_audio_ms": value}},
        )
    assert response.status_code == 200
    assert synth.plans[0].max_audio_ms == min(int(value), chain.limits.max_output_audio_ms)
    assert type(synth.plans[0].max_audio_ms) is int
