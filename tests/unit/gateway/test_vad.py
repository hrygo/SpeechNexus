"""Bounded acoustic decisions, clocks and isolation without downloading/loading a model."""

from dataclasses import replace
from itertools import pairwise
from typing import Any

import pytest

from speechnexus_gateway.application.vad import CpuVadSession
from speechnexus_gateway.domain.vad import (
    VAD_FRAME_BYTES,
    VAD_FRAME_SAMPLES,
    EmptyVadBufferError,
    VadCloseReason,
    VadError,
    VadInferenceError,
    VadPolicy,
    VadSegment,
)
from tests.fake.vad import ScriptedVadModel

FRAME = b"\x01\x00" * VAD_FRAME_SAMPLES
POLICY = VadPolicy("1")


def feed(session: CpuVadSession, audio: bytes) -> list[VadSegment]:
    result: list[VadSegment] = []
    for offset in range(0, len(audio), 8000):
        result.extend(
            session.push(audio[offset : offset + 8000], sample_start=session.accepted_samples)
        )
        assert session.buffered_bytes <= session.buffer_limit_bytes
    return result


def finish(session: CpuVadSession) -> list[VadSegment]:
    try:
        return list(session.commit())
    except EmptyVadBufferError:
        return []


def test_onset_debounce_prefix_hysteresis_and_endpoint_silence() -> None:
    # 320 ms idle, 96 ms onset, hysteresis zone, 512 ms endpoint silence.
    model = ScriptedVadModel([0.0] * 10 + [0.9] * 3 + [0.4] * 2 + [0.1] * 16)
    session = CpuVadSession(model=model, policy=POLICY)
    segments = feed(session, FRAME * 31)
    assert len(segments) == 1
    segment = segments[0]
    assert segment.reason is VadCloseReason.VAD and segment.has_speech
    assert segment.sample_start == 10 * 512 - 300 * 16
    assert segment.sample_end == 31 * 512
    assert len(segment.pcm16) == (segment.sample_end - segment.sample_start) * 2
    assert segment.wire_sample_span == (480, 31 * 768)
    assert segment.config_revision == "1" and segment.policy_digest == POLICY.digest
    assert not finish(session)
    assert session.buffered_bytes == 0
    assert "pcm16=" not in repr(segment)


def test_short_onset_is_rejected_and_real_audio_gets_empty_success_decision() -> None:
    model = ScriptedVadModel([0.0] * 5 + [0.9] * 2 + [0.0] * 4)
    session = CpuVadSession(model=model, policy=POLICY)
    assert feed(session, FRAME * 11) == []
    [segment] = session.commit()
    assert not segment.has_speech and not segment.pcm16
    assert (segment.sample_start, segment.sample_end) == (0, 11 * 512)
    assert segment.reason is VadCloseReason.CLIENT_COMMIT
    with pytest.raises(EmptyVadBufferError):
        session.commit()


def test_continuous_speech_rolls_at_exact_hard_limit_without_loss_or_repeated_prefix() -> None:
    session = CpuVadSession(
        model=ScriptedVadModel(fallback=0.9),
        policy=replace(POLICY, max_segment_ms=1000, prefix_padding_ms=0),
    )
    audio = FRAME * 100
    segments = feed(session, audio) + finish(session)
    assert [value.sample_end - value.sample_start for value in segments] == [16000] * 3 + [3200]
    assert b"".join(value.pcm16 for value in segments) == audio
    assert [value.sequence for value in segments] == [1, 2, 3, 4]
    assert [value.reason for value in segments[:-1]] == [VadCloseReason.BUDGET_ROLLOVER] * 3
    assert all(left.sample_end == right.sample_start for left, right in pairwise(segments))
    assert session.buffered_bytes == 0


def test_long_silence_has_bounded_storage_and_only_full_automatic_windows() -> None:
    session = CpuVadSession(model=ScriptedVadModel(), policy=replace(POLICY, max_segment_ms=1000))
    segments = feed(session, FRAME * 1001)
    assert len(segments) == 32
    assert all(
        not value.has_speech and value.sample_end - value.sample_start == 16000
        for value in segments
    )
    assert session.buffered_bytes <= POLICY.prefix_samples * 2 + VAD_FRAME_BYTES
    [tail] = session.commit()
    assert (tail.sample_start, tail.sample_end) == (512000, 512512)


def test_pending_onset_crosses_idle_window_without_losing_debounce() -> None:
    model = ScriptedVadModel([0.0] * 30 + [0.9] * 8)
    session = CpuVadSession(model=model, policy=replace(POLICY, max_segment_ms=1000))
    result = feed(session, FRAME * 38) + finish(session)
    assert not result[0].has_speech
    assert result[0].sample_end == 30 * 512
    assert result[1].has_speech and result[1].sample_start == 30 * 512
    assert result[1].sample_end == 38 * 512


def test_rollover_preserves_hysteresis_across_a_brief_silence_at_the_exact_cut() -> None:
    # The end threshold continues a previously triggered utterance even though the
    # new segment never reaches the stronger onset threshold or minimum duration.
    model = ScriptedVadModel([0.9] * 31 + [0.0] + [0.4] * 2)
    session = CpuVadSession(
        model=model, policy=replace(POLICY, max_segment_ms=1000, prefix_padding_ms=0)
    )
    audio = FRAME * 34
    result = feed(session, audio) + finish(session)
    assert len(result) == 2 and all(value.has_speech for value in result)
    assert result[0].sample_end == result[1].sample_start == 16000
    assert b"".join(value.pcm16 for value in result) == audio


def test_trailing_silence_after_rollover_does_not_become_an_asr_candidate() -> None:
    model = ScriptedVadModel([0.9] * 31 + [0.0] * 20)
    session = CpuVadSession(
        model=model, policy=replace(POLICY, max_segment_ms=1000, prefix_padding_ms=0)
    )
    result = feed(session, FRAME * 51) + finish(session)
    assert result[0].has_speech
    assert all(not value.has_speech for value in result[1:])
    assert result[1].sample_end == (31 + 16) * 512
    assert result[1].reason is VadCloseReason.VAD


def test_extreme_prefix_is_clipped_to_leave_onset_room_under_the_hard_limit() -> None:
    policy = replace(POLICY, max_segment_ms=1000, prefix_padding_ms=3000)
    model = ScriptedVadModel([0.0] * 20 + [0.9] * 8)
    session = CpuVadSession(model=model, policy=policy)
    result = feed(session, FRAME * 28) + finish(session)
    assert all(value.sample_end - value.sample_start <= policy.max_samples for value in result)
    assert all(len(value.pcm16) <= policy.max_samples * 2 for value in result)
    assert any(value.has_speech for value in result)


def test_chunk_partition_does_not_change_decisions_or_sample_positions() -> None:
    scores = [0.0] * 4 + [0.9] * 10 + [0.0] * 18
    audio = FRAME * 32 + b"\x01\x00" * 13
    whole = CpuVadSession(model=ScriptedVadModel(scores), policy=POLICY)
    split = CpuVadSession(model=ScriptedVadModel(scores), policy=POLICY)
    expected = feed(whole, audio) + finish(whole)
    actual: list[VadSegment] = []
    for offset in range(0, len(audio), 134):
        actual.extend(split.push(audio[offset : offset + 134], sample_start=split.accepted_samples))
    actual += finish(split)
    assert actual == expected


def test_partial_tail_is_zero_padded_for_model_only_and_not_counted_as_input() -> None:
    model = ScriptedVadModel(fallback=0.9)
    session = CpuVadSession(model=model, policy=POLICY)
    audio = FRAME * 4 + b"\x01\x00" * 13
    result = feed(session, audio) + finish(session)
    assert session.accepted_samples == 2061
    assert result[0].sample_end == 2061 and result[0].pcm16 == audio
    assert result[0].wire_sample_span == (0, 3092)
    assert model.detectors[0].frames[-1] == b"\x01\x00" * 13 + b"\x00" * (1024 - 26)


def test_two_sessions_own_detectors_and_clearing_one_does_not_touch_the_other() -> None:
    model = ScriptedVadModel(fallback=0.9)
    first = CpuVadSession(model=model, policy=POLICY)
    second = CpuVadSession(model=model, policy=POLICY)
    assert len(model.detectors) == 2 and model.detectors[0] is not model.detectors[1]
    feed(first, FRAME * 4)
    feed(second, FRAME * 4)
    first.clear()
    assert first.accepted_samples == 2048 and first.buffered_bytes == 0
    assert model.detectors[0].resets == 1 and model.detectors[1].resets == 0
    feed(second, FRAME * 2)
    [result] = second.commit()
    assert result.sample_start == 0 and result.sample_end == 3072
    feed(first, FRAME * 4)
    [after_clear] = first.commit()
    assert after_clear.sample_start == 2048 and after_clear.sample_end == 4096


def test_clear_discards_pending_tail_without_rewinding_or_reusing_segment_sequence() -> None:
    session = CpuVadSession(model=ScriptedVadModel(fallback=0.9), policy=POLICY)
    feed(session, FRAME * 4)
    [first] = session.commit()
    feed(session, b"\x01\x00" * 7)
    session.clear()
    feed(session, FRAME * 4)
    [second] = session.commit()
    assert second.sequence == first.sequence + 1 and second.sample_start == 2055
    assert second.wire_sample_span[0] == 2055 * 3 // 2


@pytest.mark.parametrize(
    "audio,start",
    [(b"", 0), (b"x", 0), (b"\x00" * 8002, 0), (FRAME, True), (FRAME, 1), (bytearray(FRAME), 0)],
)
def test_bad_input_is_rejected_before_consuming_audio_or_model_state(
    audio: Any, start: Any
) -> None:
    model = ScriptedVadModel()
    session = CpuVadSession(model=model, policy=POLICY)
    with pytest.raises(VadError):
        session.push(audio, sample_start=start)
    assert session.accepted_samples == 0 and session.buffered_bytes == 0
    assert not model.detectors[0].frames


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.01, 1.01, True, "0.5"])
def test_bad_model_result_fails_closed_and_cannot_be_replayed_in_the_same_state(score: Any) -> None:
    session = CpuVadSession(model=ScriptedVadModel([score]), policy=POLICY)
    with pytest.raises(VadInferenceError, match="cannot be continued"):
        session.push(FRAME, sample_start=0)
    assert session.buffered_bytes == 0
    with pytest.raises(VadError, match="new stream epoch"):
        session.push(FRAME, sample_start=session.accepted_samples)


def test_close_is_terminal_and_does_not_infer_asr_completion_or_resource_release() -> None:
    session = CpuVadSession(model=ScriptedVadModel(), policy=POLICY)
    feed(session, FRAME * 2)
    session.close()
    assert session.buffered_bytes == 0
    with pytest.raises(VadError):
        session.commit()


@pytest.mark.parametrize("operation", ["score", "clear", "close"])
def test_vendor_failures_do_not_expose_details_or_allow_session_continuation(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    model = ScriptedVadModel()
    session = CpuVadSession(model=model, policy=POLICY)

    def fail(*args: Any) -> Any:
        raise RuntimeError("private vendor details and content")

    detector = model.detectors[0]
    monkeypatch.setattr(detector, "reset", fail)
    if operation == "score":
        monkeypatch.setattr(detector, "probability", fail)
    with pytest.raises(VadInferenceError) as error:
        if operation == "score":
            session.push(FRAME, sample_start=0)
        elif operation == "clear":
            session.clear()
        else:
            session.close()
    assert "private" not in str(error.value) and session.buffered_bytes == 0
    with pytest.raises(VadError, match="new stream epoch"):
        session.commit()


@pytest.mark.parametrize(
    "changes",
    [
        {"config_revision": ""},
        {"threshold": True},
        {"threshold": float("nan")},
        {"end_threshold": 0.6},
        {"min_speech_ms": 31},
        {"prefix_padding_ms": 3001},
        {"silence_duration_ms": 199},
        {"max_segment_ms": 30001},
        {"max_segment_ms": True},
    ],
)
def test_policy_is_strict_and_versioned(changes: dict[str, Any]) -> None:
    with pytest.raises(VadError):
        replace(POLICY, **changes)


def test_policy_digest_changes_when_configuration_or_algorithm_parameters_change() -> None:
    assert replace(POLICY, config_revision="2").digest != POLICY.digest
    assert replace(POLICY, threshold=0.6).digest != POLICY.digest
