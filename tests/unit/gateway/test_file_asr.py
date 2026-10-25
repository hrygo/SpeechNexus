"""Real synthetic codec/VAD composition with a scripted recognition port."""

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from speechnexus_gateway.application.file_asr import FileAsrTranscription
from speechnexus_gateway.application.file_asr_audio import FileAsrAudio
from speechnexus_gateway.domain.asr import (
    AsrAudio,
    AsrCallError,
    AsrFailure,
    AsrPolicy,
    AsrText,
)
from speechnexus_gateway.domain.media import AudioDecodeLimits, AudioSourceFormat
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from tests.fake.vad import ScriptedVadModel
from tests.unit.gateway.test_audio_decode import wav_source

POLICY = AsrPolicy("config-1")


class Recognizer:
    binding_revision_id = "binding-1"

    def __init__(self) -> None:
        self.requests: list[AsrAudio] = []
        self.fail = False
        self.mutate: str | None = None

    async def transcribe(
        self, audio: AsrAudio, policy: AsrPolicy, *, cancelled: Callable[[], bool]
    ) -> AsrText:
        assert not cancelled()
        self.requests.append(audio)
        if self.fail:
            raise AsrCallError(AsrFailure.TRANSPORT_UNKNOWN, request_may_have_run=True)
        result = AsrText(
            audio.request_id, "engine-" + audio.request_id, "Synthetic.", 3, policy.digest
        )
        if self.mutate:
            changes: dict[str, Any] = {self.mutate: "synthetic-invalid"}
            result = replace(result, **changes)
        return result


def audio(
    *,
    speech: bool = True,
    samples: int = 48013,
    revision: str = "config-1",
    monotonic: Callable[[], float] = time.monotonic,
) -> FileAsrAudio:
    return FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=ScriptedVadModel(fallback=0.9 if speech else 0.0),
        source=wav_source(samples=samples),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        vad_policy=VadPolicy(revision, max_segment_ms=1000),
        cancelled=lambda: False,
        monotonic=monotonic,
    )


def pipeline(
    recognizer: Recognizer, *, source: FileAsrAudio | None = None, **kwargs: Any
) -> FileAsrTranscription:
    return FileAsrTranscription(
        audio=source or audio(),
        recognizer=recognizer,
        execution_id="execution-1",
        policy=POLICY,
        cancelled=kwargs.pop("cancelled", lambda: False),
        **kwargs,
    )


async def test_file_recognition_emits_text_with_actual_source_spans_and_frozen_provenance() -> None:
    recognizer = Recognizer()
    prepared = pipeline(recognizer)
    initial_summary = prepared.summary
    assert initial_summary is None
    result = [segment async for segment in prepared]
    assert prepared.summary is not None and prepared.summary.source_samples == 48013
    assert len(result) == len(recognizer.requests) == 2
    assert result[0].source_sample_span == (0, 48000)
    assert result[1].source_sample_span == (48000, 48013)
    assert all(segment.text == "Synthetic." and not segment.no_speech for segment in result)
    assert all(segment.binding_revision_id == "binding-1" for segment in result)
    assert all(segment.policy_digest == POLICY.digest for segment in result)
    assert all(
        re.fullmatch(r"nexus-[0-9a-f]{64}", value.request_id) for value in recognizer.requests
    )
    assert recognizer.requests[0].request_id != recognizer.requests[1].request_id
    assert all("Synthetic." not in repr(segment) for segment in result)
    assert all(not hasattr(segment, "pcm16") for segment in result)
    await prepared.aclose()


async def test_silence_has_explicit_empty_results_and_makes_no_engine_calls() -> None:
    recognizer = Recognizer()
    prepared = pipeline(recognizer, source=audio(speech=False, samples=48000 * 3 + 13))
    result = [segment async for segment in prepared]
    assert prepared.summary is not None and not recognizer.requests
    assert all(segment.no_speech and segment.text == "" for segment in result)
    assert all(
        segment.request_id is None and segment.engine_request_id is None for segment in result
    )
    assert result[-1].source_sample_span[1] == 48000 * 3 + 13


async def test_unknown_send_is_not_retried_or_converted_to_complete_summary() -> None:
    recognizer = Recognizer()
    recognizer.fail = True
    prepared = pipeline(recognizer)
    with pytest.raises(AsrCallError) as error:
        _ = [segment async for segment in prepared]
    assert error.value.request_may_have_run and len(recognizer.requests) == 1
    assert prepared.summary is None
    await prepared.aclose()


async def test_configuration_mismatch_is_rejected_before_any_recognition() -> None:
    recognizer = Recognizer()
    prepared = pipeline(recognizer, source=audio(revision="another-config"))
    with pytest.raises(ValueError, match="same frozen config"):
        _ = [segment async for segment in prepared]
    assert prepared.summary is None and not recognizer.requests


async def test_final_window_includes_time_after_segment_closed_before_dispatch() -> None:
    recognizer = Recognizer()
    prepared = pipeline(
        recognizer,
        source=audio(monotonic=lambda: 50.0),
        final_deadline_seconds=0.75,
        monotonic=lambda: 51.0,
    )
    with pytest.raises(AsrCallError) as error:
        _ = [s async for s in prepared]
    assert error.value.code is AsrFailure.DEADLINE_EXCEEDED
    assert not error.value.request_may_have_run
    assert not recognizer.requests and prepared.summary is None


async def test_final_window_interrupts_a_nonresponding_recognizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = Recognizer()
    interrupted = asyncio.Event()
    never = asyncio.Event()

    async def stalled(
        request: AsrAudio, policy: AsrPolicy, *, cancelled: Callable[[], bool]
    ) -> AsrText:
        recognizer.requests.append(request)
        try:
            await never.wait()
            raise AssertionError("unexpected release")
        finally:
            interrupted.set()

    monkeypatch.setattr(recognizer, "transcribe", stalled)
    prepared = pipeline(
        recognizer,
        source=audio(monotonic=lambda: 50.0),
        final_deadline_seconds=0.01,
        monotonic=lambda: 50.0,
    )
    with pytest.raises(AsrCallError) as error:
        _ = [s async for s in prepared]
    assert error.value.code is AsrFailure.DEADLINE_EXCEEDED
    assert error.value.request_may_have_run and interrupted.is_set()
    assert len(recognizer.requests) == 1 and prepared.summary is None


@pytest.mark.parametrize("field", ["request_id", "policy_digest", "completion_tokens"])
async def test_mismatched_native_recognizer_result_is_not_accepted(field: str) -> None:
    recognizer = Recognizer()
    recognizer.mutate = field
    prepared = pipeline(recognizer)
    with pytest.raises(AsrCallError) as error:
        _ = [segment async for segment in prepared]
    assert error.value.code == AsrFailure.RESPONSE_INVALID and prepared.summary is None


async def test_partial_close_never_claims_whole_file_completion() -> None:
    recognizer = Recognizer()
    prepared = pipeline(recognizer, source=audio(samples=48000 * 4))
    assert (await prepared.__anext__()).text == "Synthetic."
    await prepared.aclose()
    await prepared.aclose()
    assert prepared.summary is None and len(recognizer.requests) == 1
    assert [value async for value in prepared] == []


async def test_cancel_between_segments_retains_the_fact_that_recognition_already_ran() -> None:
    recognizer = Recognizer()
    cancel = False
    prepared = pipeline(recognizer, source=audio(samples=48000 * 4), cancelled=lambda: cancel)
    await prepared.__anext__()
    cancel = True
    with pytest.raises(AsrCallError) as error:
        await prepared.__anext__()
    assert error.value.request_may_have_run and len(recognizer.requests) == 1
    assert prepared.summary is None


async def test_media_cancellation_preserves_prior_engine_call_evidence() -> None:
    recognizer = Recognizer()
    cancel = False
    prepared_audio = FileAsrAudio(
        decoder=PyAvAudioDecoder(),
        model=ScriptedVadModel(fallback=0.9),
        source=wav_source(samples=48000 * 4),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        vad_policy=VadPolicy("config-1", max_segment_ms=1000),
        cancelled=lambda: cancel,
    )
    prepared = pipeline(recognizer, source=prepared_audio, cancelled=lambda: cancel)
    await prepared.__anext__()
    cancel = True
    with pytest.raises(AsrCallError) as error:
        await prepared.__anext__()
    assert error.value.code == AsrFailure.CANCELLED and error.value.request_may_have_run
    assert len(recognizer.requests) == 1 and prepared.summary is None


async def test_binding_change_between_segments_does_not_send_to_a_replacement_model() -> None:
    recognizer = Recognizer()
    prepared = pipeline(recognizer, source=audio(samples=48000 * 4))
    await prepared.__anext__()
    recognizer.binding_revision_id = "another-binding"
    with pytest.raises(ValueError, match="binding cannot change"):
        await prepared.__anext__()
    assert len(recognizer.requests) == 1 and prepared.summary is None
