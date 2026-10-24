"""Vendor boundary injection proves control checks and private resource cleanup."""

import io
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

import av
import numpy as np
import pytest

from speechnexus_gateway.domain.media import (
    AudioDecodeLimits,
    AudioSourceFormat,
    MediaError,
    MediaLimitError,
    MediaTimelineError,
)
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder


def frame(*, rate: int = 16000, pts: int | None = 0) -> av.AudioFrame:
    result = av.AudioFrame.from_ndarray(
        np.zeros((1, 512), dtype="<i2"), format="s16", layout="mono"
    )
    result.sample_rate = rate
    result.pts = pts
    result.time_base = Fraction(1, rate)
    return result


class FakeStreams(list[Any]):
    @property
    def audio(self) -> FakeStreams:
        return self


class FakeContainer:
    def __init__(self, frames: list[av.AudioFrame], *, audio_streams: int = 1) -> None:
        self.frames = frames
        self.streams = FakeStreams(
            SimpleNamespace(codec_context=SimpleNamespace(thread_count=0))
            for _ in range(audio_streams)
        )
        self.closed = False
        self.packet_bytes = 64

    def __enter__(self) -> FakeContainer:
        return self

    def __exit__(self, *args: Any) -> None:
        self.closed = True

    def demux(self, stream: Any) -> list[Any]:
        return [SimpleNamespace(size=self.packet_bytes, decode=lambda: self.frames)]


def injected(container: FakeContainer, *, open_action: Any = None) -> PyAvAudioDecoder:
    decoder = PyAvAudioDecoder()

    def open_(source: Any, **kwargs: Any) -> FakeContainer:
        assert source.name == "input" and source.seekable()
        assert kwargs["format"] == "wav"
        assert kwargs["options"]["protocol_whitelist"] == "pipe"
        assert kwargs["options"]["err_detect"] == "explode"
        if open_action is not None:
            open_action(source, kwargs)
        return container

    decoder._av = SimpleNamespace(open=open_, logging=av.logging, AudioResampler=av.AudioResampler)
    return decoder


@pytest.mark.parametrize("change", ["gap", "rate", "missing_timestamp"])
def test_source_discontinuities_fail_and_close_the_codec(change: str) -> None:
    second = frame(pts=512)
    if change == "gap":
        second.pts = 5000
    elif change == "rate":
        second = frame(rate=24000, pts=512)
    else:
        second.pts = None
    container = FakeContainer([frame(), second])
    stream = injected(container).open(
        io.BytesIO(b"synthetic"),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        cancelled=lambda: False,
    )
    with pytest.raises(MediaTimelineError):
        list(stream)
    assert container.closed and stream.summary is None


def test_nonzero_original_origin_is_preserved_while_model_samples_start_at_zero() -> None:
    container = FakeContainer([frame(pts=1600), frame(pts=2112)])
    stream = injected(container).open(
        io.BytesIO(b"synthetic"),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        cancelled=lambda: False,
    )
    blocks = list(stream)
    assert stream.summary is not None and container.closed
    assert stream.summary.clock.first_timestamp == Fraction(1, 10)
    assert blocks[0].sample_start == 0 and blocks[-1].sample_end == 1024


def test_unknown_timestamps_remain_unknown_instead_of_becoming_a_fabricated_origin() -> None:
    container = FakeContainer([frame(pts=None), frame(pts=None)])
    stream = injected(container).open(
        io.BytesIO(b"synthetic"),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        cancelled=lambda: False,
    )
    assert list(stream)
    assert stream.summary is not None and stream.summary.clock.first_timestamp is None


def test_secondary_resource_callback_rejects_a_vendor_attempt_to_open_a_url() -> None:
    def secondary(source: Any, options: Any) -> None:
        options["io_open"]("https://untrusted.invalid/private", 0, {})

    stream = injected(FakeContainer([frame()]), open_action=secondary).open(
        io.BytesIO(b"synthetic"),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        cancelled=lambda: False,
    )
    with pytest.raises(MediaError, match="secondary audio resources") as error:
        list(stream)
    assert "untrusted" not in str(error.value) and stream.summary is None


def test_read_calls_are_bounded_even_when_the_vendor_requests_the_whole_file() -> None:
    def read_large(source: Any, options: Any) -> None:
        assert len(source.read(10**9)) == 65536

    stream = injected(FakeContainer([frame()]), open_action=read_large).open(
        io.BytesIO(b"\x00" * 131072),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        cancelled=lambda: False,
    )
    assert list(stream) and stream.summary is not None


def test_repeated_seeks_cannot_bypass_the_read_amplification_limit() -> None:
    def reread(source: Any, options: Any) -> None:
        for _ in range(10):
            source.seek(0)
            source.read(65536)

    stream = injected(FakeContainer([frame()]), open_action=reread).open(
        io.BytesIO(b"\x00" * 65536),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        cancelled=lambda: False,
    )
    with pytest.raises(MediaLimitError, match="amplification"):
        list(stream)
    assert stream.summary is None


@pytest.mark.parametrize("kind", ["streams", "packet"])
def test_stream_count_and_packet_caps_are_applied_before_decoding(kind: str) -> None:
    container = FakeContainer([frame()], audio_streams=2 if kind == "streams" else 1)
    if kind == "packet":
        container.packet_bytes = 8 * 1024 * 1024 + 1
    stream = injected(container).open(
        io.BytesIO(b"synthetic"),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        cancelled=lambda: False,
    )
    with pytest.raises(MediaError):
        list(stream)
    assert container.closed and stream.summary is None
