"""Actual codec/resampler boundaries using small synthetic audio, never model downloads."""

import io
import wave
from dataclasses import replace
from fractions import Fraction
from itertools import pairwise
from typing import Any

import av
import numpy as np
import pytest
from av.audio.stream import AudioStream

from speechnexus_gateway.domain.media import (
    AudioDecodeLimits,
    AudioSourceFormat,
    MediaCancelledError,
    MediaError,
    MediaLimitError,
    MediaTimelineError,
    PcmBlock,
    SourceAudioClock,
)
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder

LIMITS = AudioDecodeLimits()


def wav_source(rate: int = 48000, channels: int = 2, samples: int = 48013) -> io.BytesIO:
    result = io.BytesIO()
    with wave.open(result, "wb") as output:
        output.setframerate(rate)
        output.setnchannels(channels)
        output.setsampwidth(2)
        t = np.arange(samples, dtype=np.float64) / rate
        values = np.repeat(
            (4000 * np.sin(2 * np.pi * 440 * t)).astype("<i2")[:, None], channels, axis=1
        )
        output.writeframes(values.tobytes())
    result.seek(0)
    return result


def encoded_source(container_format: str, codec: str) -> io.BytesIO:
    result = io.BytesIO()
    with av.open(result, "w", format=container_format) as container:
        stream = container.add_stream(codec, rate=48000)
        assert isinstance(stream, AudioStream)
        stream.layout = "mono"
        values = (np.sin(np.arange(48000) * 2 * np.pi * 440 / 48000) * 0.1).astype("float32")[
            None, :
        ]
        frame = av.AudioFrame.from_ndarray(values, format="fltp", layout="mono")
        frame.sample_rate = 48000
        frame.pts = 0
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    result.seek(0)
    return result


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 24000, 44100, 48000, 96000, 192000])
@pytest.mark.parametrize("output_rate", [16000, 24000])
def test_actual_resampling_preserves_tail_source_clock_and_bounded_contiguous_blocks(
    rate: int, output_rate: int
) -> None:
    count = rate + 13
    source = wav_source(rate, samples=count)
    stream = PyAvAudioDecoder().open(
        source,
        source_format=AudioSourceFormat("audio/wav", rate, 2),
        limits=LIMITS,
        output_sample_rate_hz=output_rate,
        cancelled=lambda: False,
    )
    initial_summary = stream.summary
    assert initial_summary is None
    blocks = list(stream)
    summary = stream.summary
    assert summary is not None
    assert summary.source_samples == count and summary.clock.sample_rate_hz == rate
    assert summary.clock.channels == 2 and summary.clock.first_timestamp == 0
    assert summary.encoded_bytes == len(source.getvalue())
    assert blocks[0].sample_start == 0
    assert all(len(block.pcm16) <= output_rate // 10 * 2 for block in blocks)
    assert all(left.sample_end == right.sample_start for left, right in pairwise(blocks))
    assert summary.output_samples == blocks[-1].sample_end
    assert abs(summary.output_samples - Fraction(count * output_rate, rate)) <= 1
    assert summary.duration_ms == (count * 1000 + rate - 1) // rate
    first, last = summary.clock.coverage(
        0, summary.output_samples, rate_hz=output_rate, source_samples=count
    )
    assert first == 0 and count - 12 <= last <= count
    assert not source.closed
    assert all("pcm16=" not in repr(block) for block in blocks)


@pytest.mark.parametrize(
    "container,codec,media",
    [
        ("mp3", "libmp3lame", "audio/mpeg"),
        ("flac", "flac", "audio/flac"),
        ("ogg", "libopus", "audio/ogg"),
    ],
)
def test_compressed_audio_is_actually_decoded_and_original_timestamp_is_retained(
    container: str, codec: str, media: str
) -> None:
    source = encoded_source(container, codec)
    stream = PyAvAudioDecoder().open(
        source, source_format=AudioSourceFormat(media), limits=LIMITS, cancelled=lambda: False
    )
    blocks = list(stream)
    assert blocks and stream.summary is not None
    assert stream.summary.clock.sample_rate_hz == 48000
    assert stream.summary.source_samples == 48000
    assert stream.summary.output_samples == 16000
    assert any(np.any(np.frombuffer(block.pcm16, dtype="<i2")) for block in blocks)


def test_explicit_raw_pcm_requires_complete_interleaved_frames_and_matches_file_decode() -> None:
    original = wav_source(48000, 2, 48013)
    with wave.open(original, "rb") as reader:
        raw = reader.readframes(reader.getnframes())
    decoder = PyAvAudioDecoder()
    wav = decoder.open(
        original,
        source_format=AudioSourceFormat("audio/wav"),
        limits=LIMITS,
        cancelled=lambda: False,
    )
    pcm = decoder.open(
        io.BytesIO(raw),
        source_format=AudioSourceFormat("audio/pcm", 48000, 2),
        limits=LIMITS,
        cancelled=lambda: False,
    )
    assert b"".join(block.pcm16 for block in wav) == b"".join(block.pcm16 for block in pcm)
    assert wav.summary is not None and pcm.summary is not None
    assert wav.summary.source_samples == pcm.summary.source_samples == 48013
    bad = decoder.open(
        io.BytesIO(raw + b"\x01"),
        source_format=AudioSourceFormat("audio/pcm", 48000, 2),
        limits=LIMITS,
        cancelled=lambda: False,
    )
    with pytest.raises(MediaError, match="complete interleaved"):
        list(bad)
    assert bad.summary is None


@pytest.mark.parametrize(
    "limits",
    [
        replace(LIMITS, max_encoded_bytes=40),
        replace(LIMITS, max_duration_ms=500),
        replace(LIMITS, max_frame_samples=16),
    ],
)
def test_encoded_duration_and_decoded_frame_limits_never_produce_a_complete_summary(
    limits: AudioDecodeLimits,
) -> None:
    stream = PyAvAudioDecoder().open(
        wav_source(),
        source_format=AudioSourceFormat("audio/wav"),
        limits=limits,
        cancelled=lambda: False,
    )
    with pytest.raises(MediaLimitError):
        list(stream)
    assert stream.summary is None


def test_claimed_source_format_is_checked_against_actual_audio() -> None:
    stream = PyAvAudioDecoder().open(
        wav_source(),
        source_format=AudioSourceFormat("audio/wav", 16000, 1),
        limits=LIMITS,
        cancelled=lambda: False,
    )
    with pytest.raises(MediaError, match="formats differ"):
        list(stream)
    assert stream.summary is None


def test_cancel_before_open_and_after_first_block_closes_without_completion() -> None:
    cancelled = False
    source = wav_source()
    stream = PyAvAudioDecoder().open(
        source,
        source_format=AudioSourceFormat("audio/wav"),
        limits=LIMITS,
        cancelled=lambda: cancelled,
    )
    assert next(stream).sample_start == 0
    cancelled = True
    with pytest.raises(MediaCancelledError):
        next(stream)
    assert stream.summary is None and not source.closed
    before = PyAvAudioDecoder().open(
        source, source_format=AudioSourceFormat("audio/wav"), limits=LIMITS, cancelled=lambda: True
    )
    with pytest.raises(MediaCancelledError):
        next(before)


def test_wall_deadline_is_shared_by_input_and_decode() -> None:
    now = 0.0
    stream = PyAvAudioDecoder(monotonic=lambda: now).open(
        wav_source(),
        source_format=AudioSourceFormat("audio/wav"),
        limits=LIMITS,
        cancelled=lambda: False,
    )
    next(stream)
    now = LIMITS.timeout_seconds
    with pytest.raises(MediaLimitError, match="deadline"):
        next(stream)
    assert stream.summary is None


def test_broken_media_errors_do_not_include_input_names_or_bytes() -> None:
    source = io.BytesIO(b"synthetic invalid media and private data")
    stream = PyAvAudioDecoder().open(
        source, source_format=AudioSourceFormat("audio/wav"), limits=LIMITS, cancelled=lambda: False
    )
    with pytest.raises(MediaError) as error:
        list(stream)
    assert "private" not in str(error.value) and "synthetic" not in str(error.value)
    assert stream.summary is None and not source.closed


def test_closing_mid_stream_is_not_success_and_keeps_the_input_caller_owned() -> None:
    source = wav_source()
    stream = PyAvAudioDecoder().open(
        source, source_format=AudioSourceFormat("audio/wav"), limits=LIMITS, cancelled=lambda: False
    )
    next(stream)
    stream.close()
    assert list(stream) == [] and stream.summary is None and not source.closed


@pytest.mark.parametrize(
    "factory,args",
    [
        (AudioSourceFormat, ("audio/http",)),
        (AudioSourceFormat, ("audio/pcm",)),
        (AudioSourceFormat, ("audio/wav", True, 1)),
        (SourceAudioClock, (True, 1, None)),
        (PcmBlock, (16000, 0, b"x")),
        (PcmBlock, (16000, True, b"\x00\x00")),
    ],
)
def test_native_audio_values_are_strict(factory: Any, args: tuple[Any, ...]) -> None:
    with pytest.raises(MediaError):
        factory(*args)


def test_source_mapping_uses_absolute_rational_positions_and_clips_the_final_coverage() -> None:
    clock = SourceAudioClock(44100, 2, Fraction(7, 100))
    span = clock.coverage(123456789, 123458389, rate_hz=16000, source_samples=10**10)
    assert span == (123456789 * 44100 // 16000, (123458389 * 44100 + 15999) // 16000)
    assert clock.coverage(0, 8001, rate_hz=16000, source_samples=22051) == (0, 22051)
    clock_48k = SourceAudioClock(48000, 1, None)
    assert clock_48k.coverage(16000, 16004, rate_hz=16000, source_samples=48013, final=True) == (
        48000,
        48013,
    )
    with pytest.raises(MediaTimelineError, match="rounding envelope"):
        clock_48k.coverage(0, 100, rate_hz=16000, source_samples=48000, final=True)


@pytest.mark.parametrize("channels", [1, 4, 8])
def test_supported_encoded_channel_counts_are_mixed_to_mono(channels: int) -> None:
    source = wav_source(channels=channels)
    stream = PyAvAudioDecoder().open(
        source,
        source_format=AudioSourceFormat("audio/wav"),
        limits=LIMITS,
        cancelled=lambda: False,
    )
    blocks = list(stream)
    assert blocks and stream.summary is not None
    assert stream.summary.clock.channels == channels
    assert len(b"".join(block.pcm16 for block in blocks)) == stream.summary.output_samples * 2


def test_unsafe_verbose_vendor_logging_is_rejected_before_reading_audio() -> None:
    source = wav_source()
    prior = av.logging.get_level()
    try:
        av.logging.set_level(av.logging.DEBUG)
        stream = PyAvAudioDecoder().open(
            source,
            source_format=AudioSourceFormat("audio/wav"),
            limits=LIMITS,
            cancelled=lambda: False,
        )
        with pytest.raises(MediaError, match="logging must remain disabled"):
            list(stream)
        assert source.tell() == 0 and stream.summary is None
    finally:
        av.logging.set_level(prior)


def test_restored_ffmpeg_stderr_callback_is_silenced_at_the_next_checkpoint(
    capfd: pytest.CaptureFixture[str],
) -> None:
    prior = av.logging.get_level()
    try:
        av.logging.set_level(None)
        av.logging.restore_default_callback()
        assert av.logging.get_level() is None
        stream = PyAvAudioDecoder().open(
            wav_source(),
            source_format=AudioSourceFormat("audio/wav"),
            limits=LIMITS,
            cancelled=lambda: False,
        )
        assert list(stream) and stream.summary is not None
        av.logging.log(av.logging.ERROR, "synthetic", "synthetic private metadata")
        assert not capfd.readouterr().err
    finally:
        av.logging.set_level(prior)
