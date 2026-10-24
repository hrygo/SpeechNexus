"""Bounded local delivery encoding for already synthesized PCM16; no engine access."""

from __future__ import annotations

import hashlib
import importlib
import io
import wave
from collections.abc import Callable
from typing import Any

from speechnexus_gateway.domain.tts import (
    TtsAudio,
    TtsDelivery,
    TtsDeliveryError,
    TtsDeliveryFailure,
    TtsDeliveryFormat,
)

_MEDIA_TYPES = {
    TtsDeliveryFormat.WAV: "audio/wav",
    TtsDeliveryFormat.MP3: "audio/mpeg",
    TtsDeliveryFormat.PCM: "audio/pcm",
}
_MP3_BIT_RATE = 64_000
_MP3_CHUNK_SAMPLES = 24000


class LocalTtsEncoder:
    """Mono PCM16 delivery encoding with explicit size, time and cancel bounds."""

    def encode(
        self,
        audio: TtsAudio,
        *,
        target: TtsDeliveryFormat,
        max_payload_bytes: int,
        cancelled: Callable[[], bool],
    ) -> TtsDelivery:
        if type(max_payload_bytes) is not int or not 1024 <= max_payload_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid TTS delivery budget")
        if not isinstance(target, TtsDeliveryFormat):
            raise ValueError("unsupported TTS delivery format")
        if cancelled():
            raise TtsDeliveryError(TtsDeliveryFailure.CANCELLED)
        if target is TtsDeliveryFormat.PCM:
            payload = self._bounded(audio.pcm16, max_payload_bytes)
        elif target is TtsDeliveryFormat.WAV:
            payload = self._bounded(self._wav(audio), max_payload_bytes)
        else:
            payload = self._mp3(audio, max_payload_bytes, cancelled)
        if cancelled():
            raise TtsDeliveryError(TtsDeliveryFailure.CANCELLED)
        return TtsDelivery(
            target,
            _MEDIA_TYPES[target],
            payload,
            audio.sample_rate_hz,
            audio.channels,
            audio.duration_ms,
            "sha256:" + hashlib.sha256(audio.pcm16).hexdigest(),
        )

    @staticmethod
    def _bounded(payload: bytes, max_payload_bytes: int) -> bytes:
        if len(payload) > max_payload_bytes:
            raise TtsDeliveryError(TtsDeliveryFailure.LIMIT_EXCEEDED, payload_bytes=len(payload))
        return payload

    @staticmethod
    def _wav(audio: TtsAudio) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(audio.channels)
            writer.setsampwidth(2)
            writer.setframerate(audio.sample_rate_hz)
            writer.writeframes(audio.pcm16)
        return output.getvalue()

    def _mp3(
        self,
        audio: TtsAudio,
        max_payload_bytes: int,
        cancelled: Callable[[], bool],
    ) -> bytes:
        try:
            av: Any = importlib.import_module("av")
        except ImportError:
            raise TtsDeliveryError(TtsDeliveryFailure.CODEC_UNAVAILABLE) from None
        output = io.BytesIO()
        try:
            with av.open(output, mode="w", format="mp3") as container:
                stream = container.add_stream("libmp3lame", rate=audio.sample_rate_hz)
                stream.layout = "mono"
                stream.bit_rate = _MP3_BIT_RATE
                step = _MP3_CHUNK_SAMPLES * 2
                for start in range(0, len(audio.pcm16), step):
                    if cancelled():
                        raise TtsDeliveryError(TtsDeliveryFailure.CANCELLED)
                    block = audio.pcm16[start : start + step]
                    frame = av.AudioFrame(format="s16", layout="mono", samples=len(block) // 2)
                    frame.sample_rate = audio.sample_rate_hz
                    frame.planes[0].update(block)
                    for packet in stream.encode(frame):
                        container.mux(packet)
                    if output.tell() > max_payload_bytes:
                        raise TtsDeliveryError(
                            TtsDeliveryFailure.LIMIT_EXCEEDED,
                            payload_bytes=output.tell(),
                        )
                for packet in stream.encode(None):
                    container.mux(packet)
        except TtsDeliveryError:
            raise
        except Exception:
            raise TtsDeliveryError(TtsDeliveryFailure.ENCODE_FAILED) from None
        return self._bounded(output.getvalue(), max_payload_bytes)
