"""Bounded local delivery encoding for synthesized PCM16."""

import io
import wave
from typing import Any

import av
import pytest

from speechnexus_gateway.domain.tts import (
    TtsAudio,
    TtsDeliveryError,
    TtsDeliveryFailure,
    TtsDeliveryFormat,
    TtsPolicy,
)
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder

POLICY = TtsPolicy("config-1", "Vivian")
PCM = b"\x01\x00" * 24000
AUDIO = TtsAudio("request-1", "audio/wav", PCM, 24000, 1, POLICY.digest)


def encoder() -> LocalTtsEncoder:
    return LocalTtsEncoder()


def test_pcm_delivery_is_the_exact_normalized_bytes() -> None:
    delivery = encoder().encode(
        AUDIO,
        target=TtsDeliveryFormat.PCM,
        max_payload_bytes=len(PCM),
        cancelled=lambda: False,
    )
    assert delivery.payload == PCM
    assert delivery.media_type == "audio/pcm"
    assert delivery.duration_ms == 1000
    assert delivery.source_pcm_digest.startswith("sha256:")


def test_wav_delivery_round_trips_through_a_standard_reader() -> None:
    delivery = encoder().encode(
        AUDIO,
        target=TtsDeliveryFormat.WAV,
        max_payload_bytes=len(PCM) + 4096,
        cancelled=lambda: False,
    )
    assert delivery.media_type == "audio/wav"
    with wave.open(io.BytesIO(delivery.payload), "rb") as reader:
        assert reader.getframerate() == 24000
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getnframes() == len(PCM) // 2
        assert reader.readframes(len(PCM) // 2) == PCM


def test_mp3_delivery_decodes_back_to_the_same_sample_clock() -> None:
    delivery = encoder().encode(
        AUDIO,
        target=TtsDeliveryFormat.MP3,
        max_payload_bytes=1024 * 1024,
        cancelled=lambda: False,
    )
    assert delivery.media_type == "audio/mpeg"
    assert delivery.payload[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")
    with av.open(io.BytesIO(delivery.payload)) as opened:
        container: Any = opened
        stream = container.streams.audio[0]
        samples = sum(int(frame.samples) for frame in container.decode(stream))
        rate = int(stream.rate)
    assert rate == 24000
    assert samples == len(PCM) // 2


class Counter:
    def __init__(self, *, allow: int) -> None:
        self.remaining = allow

    def check(self) -> bool:
        self.remaining -= 1
        return self.remaining < 0


def test_payload_budget_rejects_oversized_delivery() -> None:
    with pytest.raises(TtsDeliveryError) as error:
        encoder().encode(
            AUDIO,
            target=TtsDeliveryFormat.PCM,
            max_payload_bytes=1024,
            cancelled=lambda: False,
        )
    assert error.value.code == TtsDeliveryFailure.LIMIT_EXCEEDED
    assert error.value.payload_bytes == len(PCM)


def test_cancel_before_encode_does_no_work() -> None:
    with pytest.raises(TtsDeliveryError) as error:
        encoder().encode(
            AUDIO,
            target=TtsDeliveryFormat.MP3,
            max_payload_bytes=1024 * 1024,
            cancelled=lambda: True,
        )
    assert error.value.code == TtsDeliveryFailure.CANCELLED


def test_cancel_between_mp3_chunks_stops_encoding() -> None:
    counter = Counter(allow=2)
    with pytest.raises(TtsDeliveryError) as error:
        encoder().encode(
            AUDIO,
            target=TtsDeliveryFormat.MP3,
            max_payload_bytes=1024 * 1024,
            cancelled=counter.check,
        )
    assert error.value.code == TtsDeliveryFailure.CANCELLED


def test_missing_codec_runtime_is_an_explicit_delivery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_: str) -> object:
        raise ImportError("synthetic missing codec")

    monkeypatch.setattr(
        "speechnexus_gateway.infrastructure.local_tts_encoder.importlib.import_module",
        missing,
    )
    with pytest.raises(TtsDeliveryError) as error:
        encoder().encode(
            AUDIO,
            target=TtsDeliveryFormat.MP3,
            max_payload_bytes=1024 * 1024,
            cancelled=lambda: False,
        )
    assert error.value.code == TtsDeliveryFailure.CODEC_UNAVAILABLE


@pytest.mark.parametrize("budget", [10, 0, 65 * 1024 * 1024])
def test_invalid_delivery_budgets_are_rejected(budget: int) -> None:
    with pytest.raises(ValueError):
        encoder().encode(
            AUDIO,
            target=TtsDeliveryFormat.PCM,
            max_payload_bytes=budget,
            cancelled=lambda: False,
        )


def test_unknown_delivery_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        encoder().encode(
            AUDIO,
            target="opus",  # type: ignore[arg-type]
            max_payload_bytes=1024,
            cancelled=lambda: False,
        )
