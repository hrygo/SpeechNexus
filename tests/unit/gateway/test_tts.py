"""Native TTS policy, request and delivery facts plus single-use orchestration."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from speechnexus_gateway.application.file_tts import FileTtsSynthesis
from speechnexus_gateway.domain.tts import (
    MAX_TTS_TEXT_CHARS,
    TtsAudio,
    TtsCallError,
    TtsDelivery,
    TtsDeliveryError,
    TtsDeliveryFailure,
    TtsDeliveryFormat,
    TtsFailure,
    TtsPolicy,
    TtsRequest,
)

POLICY = TtsPolicy("config-1", "Vivian")
PCM = b"\x01\x00" * 2400
AUDIO = TtsAudio("request-1", "audio/wav", PCM, 24000, 1, POLICY.digest)
DELIVERY = TtsDelivery(
    TtsDeliveryFormat.WAV,
    "audio/wav",
    b"RIFF" + b"\x00" * 40,
    24000,
    1,
    100,
    "sha256:" + "0" * 64,
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"config_revision": ""},
        {"config_revision": "bad revision"},
        {"voice": "bad voice"},
        {"speed": 0.1},
        {"speed": float("nan")},
        {"sample_rate_hz": 16000},
        {"max_pcm_bytes": 10},
        {"max_audio_ms": 0},
        {"timeout_seconds": 0},
        {"instructions": "bad\x00instruction"},
    ],
)
def test_invalid_frozen_policies_are_rejected(kwargs: dict[str, Any]) -> None:
    fields: dict[str, Any] = {"config_revision": "config-1", "voice": "Vivian", **kwargs}
    with pytest.raises(ValueError):
        TtsPolicy(**fields)


def test_policy_digest_tracks_every_frozen_field() -> None:
    other = replace(POLICY, instructions="柔和一些", speed=1.25, sample_rate_hz=8000)
    assert POLICY.digest != other.digest
    assert POLICY.digest == TtsPolicy("config-1", "Vivian").digest
    assert POLICY.digest.startswith("sha256:")


@pytest.mark.parametrize(
    "text",
    ["", "x" * (MAX_TTS_TEXT_CHARS + 1), "bad\x00text", "\ud800"],
)
def test_invalid_requests_are_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        TtsRequest("request-1", text)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TtsAudio("request-1", "audio/wav", b"\x00\x00\x00", 24000, 1, POLICY.digest),
        lambda: TtsAudio("request-1", "audio/wav", b"", 24000, 1, POLICY.digest),
        lambda: TtsAudio("request-1", "audio/wav", PCM, 24000, 2, POLICY.digest),
        lambda: TtsAudio("request-1", "audio/wav", PCM, 16000, 1, POLICY.digest),
        lambda: TtsAudio("request-1", "audio/flac", PCM, 24000, 1, POLICY.digest),
    ],
)
def test_invalid_normalized_audio_is_rejected(
    factory: Callable[[], TtsAudio],
) -> None:
    with pytest.raises(ValueError):
        factory()


def test_duration_is_derived_from_the_declared_sample_clock() -> None:
    assert AUDIO.duration_ms == 100
    assert replace(AUDIO, sample_rate_hz=8000).duration_ms == 300


class FakeSynthesizer:
    def __init__(
        self,
        *,
        audio: TtsAudio | None = None,
        error: TtsCallError | None = None,
        binding_revision_id: str = "binding-1",
    ) -> None:
        self.audio = audio or AUDIO
        self.error = error
        self.binding_revision_id = binding_revision_id
        self.calls: list[TtsPolicy] = []

    async def synthesize(
        self,
        request: TtsRequest,
        policy: TtsPolicy,
        *,
        cancelled: Callable[[], bool],
    ) -> TtsAudio:
        self.calls.append(policy)
        if self.error is not None:
            raise self.error
        return self.audio


class FakeEncoder:
    def __init__(
        self, *, delivery: TtsDelivery | None = None, error: Exception | None = None
    ) -> None:
        self.delivery = delivery or DELIVERY
        self.error = error
        self.calls: list[tuple[TtsDeliveryFormat, int]] = []

    def encode(
        self,
        audio: TtsAudio,
        *,
        target: TtsDeliveryFormat,
        max_payload_bytes: int,
        cancelled: Callable[[], bool],
    ) -> TtsDelivery:
        self.calls.append((target, max_payload_bytes))
        if self.error is not None:
            raise self.error
        return self.delivery


def build(
    *,
    synthesizer: FakeSynthesizer | None = None,
    encoder: FakeEncoder | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[FileTtsSynthesis, FakeSynthesizer, FakeEncoder]:
    fake_synthesizer = synthesizer or FakeSynthesizer()
    fake_encoder = encoder or FakeEncoder()
    return (
        FileTtsSynthesis(
            synthesizer=fake_synthesizer,
            encoder=fake_encoder,
            request=TtsRequest("request-1", "合成 synthetic."),
            policy=POLICY,
            delivery_format=TtsDeliveryFormat.WAV,
            max_payload_bytes=1024 * 1024,
            cancelled=cancelled or (lambda: False),
            execution_id="execution-1",
        ),
        fake_synthesizer,
        fake_encoder,
    )


async def test_one_call_produces_one_bounded_delivery() -> None:
    synthesis, synthesizer, encoder = build()
    result = await synthesis.run()
    assert result.execution_id == "execution-1"
    assert result.request_id == "request-1"
    assert result.policy_digest == POLICY.digest
    assert result.engine_media_type == "audio/wav"
    assert result.pcm_bytes == len(PCM) and result.duration_ms == 100
    assert result.delivery is DELIVERY
    assert synthesizer.calls == [POLICY]
    assert encoder.calls == [(TtsDeliveryFormat.WAV, 1024 * 1024)]
    assert synthesis.has_dispatched
    assert "synthetic" not in repr(result)


async def test_cancel_before_dispatch_never_calls_the_engine() -> None:
    synthesis, synthesizer, encoder = build(cancelled=lambda: True)
    with pytest.raises(TtsCallError) as error:
        await synthesis.run()
    assert error.value.code == TtsFailure.CANCELLED
    assert not error.value.request_may_have_run
    assert not synthesizer.calls and not encoder.calls and not synthesis.has_dispatched


async def test_inflight_engine_cancel_stays_unresolved_for_the_caller() -> None:
    engine_error = TtsCallError(TtsFailure.CANCELLED, request_may_have_run=True)
    synthesis, _, encoder = build(synthesizer=FakeSynthesizer(error=engine_error))
    with pytest.raises(TtsCallError) as error:
        await synthesis.run()
    assert error.value.request_may_have_run and synthesis.has_dispatched
    assert not encoder.calls


@pytest.mark.parametrize(
    "audio",
    [
        TtsAudio("another-request", "audio/wav", PCM, 24000, 1, POLICY.digest),
        TtsAudio("request-1", "audio/wav", PCM, 24000, 1, "sha256:another"),
        TtsAudio("request-1", "audio/wav", PCM, 8000, 1, POLICY.digest),
    ],
)
async def test_engine_results_must_match_the_frozen_identity(audio: TtsAudio) -> None:
    synthesis, _, _ = build(synthesizer=FakeSynthesizer(audio=audio))
    with pytest.raises(TtsCallError) as error:
        await synthesis.run()
    assert error.value.code == TtsFailure.RESPONSE_INVALID
    assert error.value.request_may_have_run


async def test_binding_revision_cannot_change_within_an_execution() -> None:
    synthesis, synthesizer, _ = build()
    synthesizer.binding_revision_id = "binding-2"
    with pytest.raises(ValueError):
        await synthesis.run()


async def test_local_encoding_failure_never_pretends_the_engine_was_stopped() -> None:
    synthesis, _, _ = build(
        encoder=FakeEncoder(
            error=TtsDeliveryError(TtsDeliveryFailure.ENCODE_FAILED, payload_bytes=1)
        )
    )
    with pytest.raises(TtsDeliveryError) as error:
        await synthesis.run()
    assert error.value.code == TtsDeliveryFailure.ENCODE_FAILED
    assert synthesis.has_dispatched


def test_delivery_rejects_empty_or_mislabeled_payloads() -> None:
    with pytest.raises(ValueError):
        TtsDelivery(TtsDeliveryFormat.WAV, "audio/wav", b"", 24000, 1, 100, "sha256:x")
    with pytest.raises(ValueError):
        TtsDelivery(TtsDeliveryFormat.WAV, "audio/wav", b"x", 24000, 1, 0, "sha256:x")
