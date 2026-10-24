"""Encoded-file/VAD composition, source clocks and cleanup without a real ASR engine."""

from dataclasses import replace

import pytest

from speechnexus_gateway.application.file_asr_audio import FileAsrAudio
from speechnexus_gateway.domain.media import (
    AudioDecodeLimits,
    AudioSourceFormat,
    MediaCancelledError,
    MediaLimitError,
)
from speechnexus_gateway.domain.vad import VadInferenceError, VadPolicy
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from tests.fake.vad import ScriptedVadDetector, ScriptedVadModel
from tests.unit.gateway.test_audio_decode import wav_source


def test_file_pipeline_preserves_short_tail_and_maps_the_final_segment_through_source_eof() -> None:
    source = wav_source()
    model = ScriptedVadModel(fallback=0.9)
    prepared = FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=model,
        source=source,
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        vad_policy=VadPolicy("1", max_segment_ms=1000),
        cancelled=lambda: False,
    )
    initial_summary = prepared.summary
    assert initial_summary is None
    segments = list(prepared)
    assert len(segments) == 2 and all(segment.vad.has_speech for segment in segments)
    assert segments[0].source_sample_span == (0, 48000)
    assert segments[1].source_sample_span == (48000, 48013)
    summary = prepared.summary
    assert summary is not None
    assert summary.source_samples == 48013 and summary.output_samples == 16004
    assert sum(len(segment.vad.pcm16) // 2 for segment in segments) == summary.output_samples
    assert not source.closed
    prepared.close()


def test_no_speech_result_contains_no_audio_for_asr_even_for_a_longer_encoded_file() -> None:
    prepared = FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=ScriptedVadModel(),
        source=wav_source(samples=48000 * 3 + 13),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        vad_policy=VadPolicy("1", max_segment_ms=1000),
        cancelled=lambda: False,
    )
    segments = list(prepared)
    assert prepared.summary is not None
    assert len(segments) == 4 and all(not value.vad.has_speech for value in segments)
    assert all(not value.vad.pcm16 for value in segments)
    assert segments[-1].source_sample_span[1] == 48000 * 3 + 13


def test_partial_iteration_cancel_discards_pending_audio_and_never_returns_a_complete_summary() -> (
    None
):
    cancel = False
    source = wav_source(samples=48000 * 4)
    model = ScriptedVadModel(fallback=0.9)
    prepared = FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=model,
        source=source,
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        vad_policy=VadPolicy("1", max_segment_ms=1000),
        cancelled=lambda: cancel,
    )
    assert next(prepared).vad.sample_start == 0
    cancel = True
    with pytest.raises(MediaCancelledError):
        next(prepared)
    assert prepared.summary is None and not source.closed
    assert model.detectors[0].resets > 0


def test_decoder_limits_do_not_publish_vad_or_media_completion() -> None:
    model = ScriptedVadModel(fallback=0.9)
    prepared = FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=model,
        source=wav_source(),
        source_format=AudioSourceFormat("audio/wav"),
        limits=replace(AudioDecodeLimits(), max_duration_ms=500),
        vad_policy=VadPolicy("1"),
        cancelled=lambda: False,
    )
    with pytest.raises(MediaLimitError):
        list(prepared)
    assert prepared.summary is None and model.detectors[0].resets > 0


def test_failed_vad_cannot_be_converted_to_no_speech_success() -> None:
    prepared = FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=ScriptedVadModel([float("nan")]),
        source=wav_source(),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        vad_policy=VadPolicy("1"),
        cancelled=lambda: False,
    )
    with pytest.raises(VadInferenceError):
        list(prepared)
    assert prepared.summary is None


def test_close_before_first_iteration_releases_private_state_without_reading_input() -> None:
    source = wav_source()
    model = ScriptedVadModel()
    prepared = FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=model,
        source=source,
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        vad_policy=VadPolicy("1"),
        cancelled=lambda: False,
    )
    prepared.close()
    prepared.close()
    assert list(prepared) == [] and prepared.summary is None
    assert source.tell() == 0 and not source.closed
    assert model.detectors[0].resets == 1 and not model.detectors[0].frames


def test_final_cleanup_failure_does_not_leave_a_successful_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reset(detector: ScriptedVadDetector) -> None:
        detector.resets += 1
        if detector.resets == 2:
            raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(ScriptedVadDetector, "reset", reset)
    prepared = FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=ScriptedVadModel(fallback=0.9),
        source=wav_source(),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        vad_policy=VadPolicy("1"),
        cancelled=lambda: False,
    )
    with pytest.raises(VadInferenceError, match="reset failed"):
        list(prepared)
    assert prepared.summary is None


def test_close_mid_stream_cleans_up_once_and_does_not_claim_completion() -> None:
    source = wav_source(samples=48000 * 4)
    model = ScriptedVadModel(fallback=0.9)
    prepared = FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=model,
        source=source,
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        vad_policy=VadPolicy("1", max_segment_ms=1000),
        cancelled=lambda: False,
    )
    assert next(prepared).vad.has_speech
    prepared.close()
    prepared.close()
    assert model.detectors[0].resets == 1
    assert list(prepared) == [] and prepared.summary is None and not source.closed
