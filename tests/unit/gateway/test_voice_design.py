"""Native VoiceDesign policy, request, stage failure and single-use orchestration."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from speechnexus_gateway.application.voice_design import VoiceDesignSynthesis
from speechnexus_gateway.domain.execution_results import ExecutionAudioStagingResult
from speechnexus_gateway.domain.file_execution import EngineRequestRejectedError
from speechnexus_gateway.domain.tts import TtsAudio, TtsDeliveryFormat
from speechnexus_gateway.domain.tts_results import PreparedTtsResult
from speechnexus_gateway.domain.voice_design import (
    MAX_VOICE_DESIGN_DESCRIPTION_CHARS,
    VoiceDesignCallError,
    VoiceDesignExecutionTarget,
    VoiceDesignFailure,
    VoiceDesignPolicy,
    VoiceDesignRejectedError,
    VoiceDesignRequest,
)
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from tests.fake.execution_command import command_example

POLICY = VoiceDesignPolicy("policy-voice-design")
REQUEST = VoiceDesignRequest("intent-voice-design", "合成一个温和的女声", "试听文本。", "zh", 7)
PCM = b"\x01\x00" * 2400
AUDIO = TtsAudio(REQUEST.request_id, "audio/wav", PCM, POLICY.sample_rate_hz, 1, POLICY.digest)


def test_only_the_voice_design_task_and_wav_clock_are_accepted() -> None:
    assert POLICY.task.value == "VoiceDesign"
    assert POLICY.sample_rate_hz == 24000
    assert POLICY.engine_format.value == "wav"
    with pytest.raises(ValueError):
        replace(POLICY, sample_rate_hz=16000)
    with pytest.raises(ValueError):
        replace(POLICY, max_audio_ms=10, max_pcm_bytes=1000)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"config_revision": ""},
        {"config_revision": "bad revision"},
        {"engine_format": "pcm"},
        {"timeout_seconds": 0},
        {"max_audio_ms": 0},
    ],
)
def test_invalid_frozen_policies_are_refused(kwargs: dict[str, Any]) -> None:
    fields: dict[str, Any] = {"config_revision": "policy-voice-design", **kwargs}
    with pytest.raises(ValueError):
        VoiceDesignPolicy(**fields)


def test_policy_digest_tracks_every_frozen_field() -> None:
    assert POLICY.digest != replace(POLICY, timeout_seconds=60).digest
    assert POLICY.digest != replace(POLICY, max_audio_ms=120000).digest
    assert POLICY.digest == VoiceDesignPolicy("policy-voice-design").digest
    assert POLICY.digest.startswith("sha256:")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"request_id": "bad id"},
        {"description": ""},
        {"description": "x" * (MAX_VOICE_DESIGN_DESCRIPTION_CHARS + 1)},
        {"description": "bad\x00text"},
        {"preview_text": ""},
        {"preview_text": "\ud800"},
        {"language": "EN_us"},
        {"seed": "7"},
        {"seed": 2**63},
    ],
)
def test_invalid_requests_are_refused(kwargs: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "request_id": "intent-voice-design",
        "description": "合成一个温和的女声",
        "preview_text": "试听文本。",
        "language": "zh",
        "seed": 7,
    }
    fields.update(kwargs)
    with pytest.raises(ValueError):
        VoiceDesignRequest(**fields)


class Synthesizer:
    def __init__(
        self,
        *,
        error: VoiceDesignCallError | None = None,
        audio: TtsAudio | None = None,
    ) -> None:
        self.binding_revision_id = "binding-voice-design"
        self.error = error
        self.audio = audio
        self.calls: list[tuple[VoiceDesignRequest, VoiceDesignPolicy]] = []

    def validate_execution_target(self, target: VoiceDesignExecutionTarget) -> None:
        assert target.pool_id == "voice-creation"

    async def synthesize(
        self,
        request: VoiceDesignRequest,
        policy: VoiceDesignPolicy,
        *,
        cancelled: Callable[[], bool],
    ) -> TtsAudio:
        assert not cancelled()
        self.calls.append((request, policy))
        if self.error is not None:
            raise self.error
        if self.audio is not None:
            return self.audio
        return replace(AUDIO, policy_digest=policy.digest)

    async def warmup(self) -> None:
        raise AssertionError("the stage never performs readiness synthesis")


class Store:
    def __init__(self) -> None:
        self.results: list[PreparedTtsResult] = []

    async def stage(
        self, result: PreparedTtsResult, *, cancelled: Callable[[], bool]
    ) -> ExecutionAudioStagingResult:
        assert not cancelled()
        self.results.append(result)
        return result.receipt("sn-voice-design", "version-1")


def build(
    *, error: VoiceDesignCallError | None = None, audio: TtsAudio | None = None
) -> tuple[VoiceDesignSynthesis, Store]:
    store = Store()
    return (
        VoiceDesignSynthesis(
            synthesizer=Synthesizer(error=error, audio=audio),
            encoder=LocalTtsEncoder(),
            request=REQUEST,
            policy=POLICY,
            delivery_format=TtsDeliveryFormat.WAV,
            max_payload_bytes=1024 * 1024,
            cancelled=lambda: False,
            execution_id="intent-voice-design",
        ),
        store,
    )


async def test_success_is_one_frozen_call_and_one_wav_delivery() -> None:
    synthesis, _ = build()
    result = await synthesis.run()

    assert synthesis.has_dispatched
    assert result.execution_id == "intent-voice-design"
    assert result.request_id == REQUEST.request_id
    assert result.policy_digest == POLICY.digest
    assert result.binding_revision_id == "binding-voice-design"
    assert result.engine_media_type == "audio/wav"
    assert result.sample_rate_hz == 24000
    assert result.channels == 1
    assert result.pcm_bytes == len(PCM)
    assert result.duration_ms == 100
    assert result.delivery.format is TtsDeliveryFormat.WAV
    assert result.delivery.payload[:4] == b"RIFF"


async def test_success_stages_exactly_one_private_receipt() -> None:
    synthesis, store = build()
    receipt = await synthesis.stage_result(
        workspace_id="ws-demo", request_digest="sha256:" + "a" * 64, store=store
    )

    assert len(store.results) == 1
    assert store.results[0].audio.source_pcm_samples == len(PCM) // 2
    assert store.results[0].media_type == "audio/wav"
    assert receipt.request_digest == "sha256:" + "a" * 64


async def test_a_second_dispatch_on_the_same_synthesis_is_refused() -> None:
    synthesis, _ = build()
    await synthesis.run()
    with pytest.raises(ValueError):
        await synthesis.run()


async def test_engine_rejection_is_terminal_and_not_a_timeout_or_transport_failure() -> None:
    synthesis, store = build(error=VoiceDesignRejectedError())
    with pytest.raises(VoiceDesignRejectedError) as raised:
        await synthesis.stage_result(
            workspace_id="ws-demo", request_digest="sha256:" + "a" * 64, store=store
        )

    error = raised.value
    assert isinstance(error, EngineRequestRejectedError)
    assert error.code is VoiceDesignFailure.ENGINE_REJECTED
    assert error.request_may_have_run is False
    assert not isinstance(error, TimeoutError)
    assert store.results == []


async def test_deadline_failure_keeps_the_unknown_transport_claim() -> None:
    synthesis, _ = build(
        error=VoiceDesignCallError(VoiceDesignFailure.DEADLINE_EXCEEDED, request_may_have_run=True)
    )
    with pytest.raises(VoiceDesignCallError) as raised:
        await synthesis.run()
    assert raised.value.code is VoiceDesignFailure.DEADLINE_EXCEEDED
    assert raised.value.request_may_have_run is True
    assert not isinstance(raised.value, EngineRequestRejectedError)


async def test_cancellation_before_dispatch_claims_nothing_ran() -> None:
    synthesis = VoiceDesignSynthesis(
        synthesizer=Synthesizer(),
        encoder=LocalTtsEncoder(),
        request=REQUEST,
        policy=POLICY,
        delivery_format=TtsDeliveryFormat.WAV,
        max_payload_bytes=1024 * 1024,
        cancelled=lambda: True,
        execution_id="intent-voice-design",
    )
    with pytest.raises(VoiceDesignCallError) as raised:
        await synthesis.run()
    assert raised.value.code is VoiceDesignFailure.CANCELLED
    assert raised.value.request_may_have_run is False


async def test_audio_that_contradicts_the_frozen_clock_is_invalid() -> None:
    synthesis, _ = build(
        audio=TtsAudio("another-request", "audio/wav", PCM, 24000, 1, POLICY.digest)
    )
    with pytest.raises(VoiceDesignCallError) as raised:
        await synthesis.run()
    assert raised.value.code is VoiceDesignFailure.RESPONSE_INVALID
    assert raised.value.request_may_have_run is True


def test_the_command_example_names_the_design_stage() -> None:
    command = command_example(2)
    assert command["kind"] == "voicedesign"
    assert command["task_kind"] == "voice_design"
    assert command["stage_role"] == "voice_design"
    assert command["description"] and command["preview_text"]
