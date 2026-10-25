"""Frozen commands drive the actual media pipeline, with synthetic inference."""

import hashlib
import io
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.domain.asr import AsrAudio, AsrCallError, AsrFailure, AsrPolicy, AsrText
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputLimits,
    ExecutionObjectReference,
    VerifiedExecutionObject,
)
from speechnexus_gateway.domain.media import AudioDecodeLimits
from speechnexus_gateway.domain.submission import InvalidExecutionRequestError, SubmitRequest
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_gateway.interfaces.file_asr_command import CommandFileAsr
from tests.fake.execution_command import command_example, original_request
from tests.fake.vad import ScriptedVadModel
from tests.unit.gateway.test_audio_decode import wav_source
from tests.unit.gateway.test_file_asr import Recognizer

TUNING = VadPolicy("policy-input", threshold=0.65, end_threshold=0.25, prefix_padding_ms=64)


class Objects:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads: list[ExecutionObjectReference] = []
        self.streams: list[io.BytesIO] = []
        self.on_open: Callable[[], None] = lambda: None

    @contextmanager
    def open_verified(
        self,
        reference: ExecutionObjectReference,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> Iterator[VerifiedExecutionObject]:
        assert not cancelled()
        self.reads.append(reference)
        with io.BytesIO(self.payload) as stream:
            self.streams.append(stream)
            self.on_open()
            yield VerifiedExecutionObject(reference, stream, "synthetic-version")


class CapturingRecognizer(Recognizer):
    binding_revision_id = "binding-input"

    def __init__(self) -> None:
        super().__init__()
        self.policies: list[AsrPolicy] = []
        self.after_call: Callable[[], None] = lambda: None

    async def transcribe(
        self, audio: AsrAudio, policy: AsrPolicy, *, cancelled: Callable[[], bool]
    ) -> AsrText:
        self.policies.append(policy)
        result = await super().transcribe(audio, policy, cancelled=cancelled)
        self.after_call()
        return result


@pytest.fixture
def setup() -> tuple[SubmitRequest, Objects, CapturingRecognizer]:
    with wav_source(samples=48013, channels=1) as source:
        payload = source.read()
    command = command_example()
    command["audio"].update({"sample_rate_hz": 48000, "channels": 1})
    command["audio"]["storage"].update(
        {
            "byte_length": len(payload),
            "content_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }
    )
    command["parameters"].update(
        {
            "language": "zh",
            "prompt": "Synthetic private prompt",
            "temperature": 0.25,
            "final_deadline_ms": 750,
            "max_segment_ms": 1000,
            "turn_detection": {"mode": "server_vad", "silence_duration_ms": 240},
        }
    )
    return original_request(command), Objects(payload), CapturingRecognizer()


def consumer(objects: Objects, recognizer: CapturingRecognizer, **kwargs: Any) -> CommandFileAsr:
    return CommandFileAsr(
        inputs=FileExecutionInputs(objects),
        decoder=PyAvAudioDecoder(),
        vad_model=ScriptedVadModel(fallback=0.9),
        recognizer=recognizer,
        vad_tuning=kwargs.pop("vad_tuning", TUNING),
        max_completion_tokens=kwargs.pop("max_completion_tokens", 64),
        max_call_seconds=kwargs.pop("max_call_seconds", 2.0),
        input_limits=ExecutionInputLimits(1000000, 1024, 10),
        decode_limits=AudioDecodeLimits(),
        **kwargs,
    )


@pytest.mark.parametrize("response_format", ["json", "text"])
@pytest.mark.parametrize("policy_revision", ["policy-input", "policy:input"])
async def test_complete_command_drives_native_policies_and_source_clock(
    setup: Any, response_format: str, policy_revision: str
) -> None:
    request, objects, recognizer = setup
    request.command["parameters"]["response_format"] = response_format
    request = original_request(request.command)
    request = replace(
        request, identity=replace(request.identity, policy_revision_id=policy_revision)
    )
    tuning = replace(TUNING, config_revision=policy_revision)
    async with consumer(objects, recognizer, vad_tuning=tuning).open(
        request, cancelled=lambda: False
    ) as run:
        initially_dispatched = run.has_dispatched
        assert not initially_dispatched
        segments = [segment async for segment in run]
        assert run.summary is not None and run.summary.source_samples == 48013
        assert run.has_dispatched and len(segments) == len(recognizer.requests) == 2
        assert segments[-1].source_sample_span[1] == 48013
        expected = AsrPolicy(
            policy_revision,
            language="zh",
            prompt="Synthetic private prompt",
            temperature=0.25,
            max_completion_tokens=64,
            timeout_seconds=0.75,
        )
        vad = replace(tuning, max_segment_ms=1000, silence_duration_ms=240)
        assert recognizer.policies == [expected, expected]
        assert all(s.policy_digest == expected.digest for s in segments)
        assert all(s.vad_policy_digest == vad.digest for s in segments)
        assert all(s.binding_revision_id == request.identity.binding_revision_id for s in segments)
        assert "Synthetic private prompt" not in repr(segments)
    assert all(s.closed for s in objects.streams)


@pytest.mark.parametrize(
    "path,value",
    [
        (("parameters", "alignment"), "required"),
        (("parameters", "alignment"), "best_effort"),
        (("parameters", "diarization"), "required"),
        (("parameters", "diarization"), "best_effort"),
        (("parameters", "vocabulary_set_revision"), "7"),
        (("parameters", "timestamp_granularities"), ["word"]),
        (("parameters", "timestamp_granularities"), ["segment"]),
        (("parameters", "response_format"), "verbose_json"),
        (("parameters", "response_format"), "srt"),
        (("parameters", "response_format"), "vtt"),
        (("parameters", "turn_detection", "mode"), "manual"),
        (("parameters", "finalization"), "streaming_finalize"),
        (("parameters", "rollback_tokens"), 1),
        (("parameters", "preview_interval_ms"), 2000),
        (("resources", "vocabulary"), [{"text": "Synthetic", "weight": 1}]),
        (("resources", "vocabulary_source"), {"id": "vocab-test", "revision": "1"}),
        (("resources", "pronunciation_source"), {"id": "pron-test", "revision": "1"}),
        (("task_kind",), "asr_realtime"),
        (("stage_role",), "asr_preview"),
        (("parameters", "prompt"), "do-not-echo\x00"),
        (("parameters", "extra"), "do-not-echo"),
    ],
)
async def test_unsupported_or_invalid_command_never_reads_or_dispatches(
    setup: Any, path: tuple[str, ...], value: Any
) -> None:
    request, objects, recognizer = setup
    command = request.command
    cursor = command
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    request = original_request(command)
    with pytest.raises(InvalidExecutionRequestError) as error:
        async with consumer(objects, recognizer).open(request, cancelled=lambda: False):
            pytest.fail("unsupported command accepted")
    assert not objects.reads and not recognizer.requests
    assert "do-not-echo" not in str(error.value)


@pytest.mark.parametrize("change", ["digest", "binding", "policy", "voice", "missing"])
async def test_identity_and_complete_contract_are_checked_before_object_io(
    setup: Any, change: str
) -> None:
    request, objects, recognizer = setup
    if change == "digest":
        request.command["parameters"]["language"] = "en"
    elif change == "missing":
        del request.command["parameters"]["language"]
        request = original_request(request.command)
    else:
        field = {
            "binding": "binding_revision_id",
            "policy": "policy_revision_id",
            "voice": "voice_revision_id",
        }[change]
        request = replace(request, identity=replace(request.identity, **{field: "another-id"}))
    with pytest.raises(InvalidExecutionRequestError):
        async with consumer(objects, recognizer).open(request, cancelled=lambda: False):
            pytest.fail("identity mismatch accepted")
    assert not objects.reads and not recognizer.requests


async def test_original_mutation_cannot_change_an_opened_execution(setup: Any) -> None:
    request, objects, recognizer = setup
    async with consumer(objects, recognizer).open(request, cancelled=lambda: False) as run:
        request.command["parameters"]["language"] = "en"
        request.command["parameters"]["prompt"] = "Replacement"
        _ = [s async for s in run]
    assert all(
        p.language == "zh" and p.prompt == "Synthetic private prompt" for p in recognizer.policies
    )


async def test_null_prompt_and_stricter_local_timeout_are_preserved(setup: Any) -> None:
    request, objects, recognizer = setup
    request.command["parameters"]["prompt"] = None
    request = original_request(request.command)
    async with consumer(objects, recognizer, max_call_seconds=0.25).open(
        request, cancelled=lambda: False
    ) as run:
        _ = [s async for s in run]
    assert all(p.prompt == "" and p.timeout_seconds == 0.25 for p in recognizer.policies)


async def test_abandoned_iterator_closes_private_input_without_inference(setup: Any) -> None:
    request, objects, recognizer = setup
    with pytest.raises(RuntimeError, match="caller failure"):
        async with consumer(objects, recognizer).open(request, cancelled=lambda: False):
            raise RuntimeError("caller failure")
    assert objects.streams and all(s.closed for s in objects.streams)
    assert not recognizer.requests


async def test_pre_cancelled_run_never_reads_input(setup: Any) -> None:
    request, objects, recognizer = setup
    with pytest.raises(AsrCallError) as error:
        async with consumer(objects, recognizer).open(request, cancelled=lambda: True):
            pytest.fail("cancelled execution opened")
    assert error.value.code is AsrFailure.CANCELLED
    assert not error.value.request_may_have_run and not objects.reads


@pytest.mark.parametrize("when", ["before-input", "during-input", "after-dispatch"])
async def test_original_absolute_deadline_is_checked_throughout_execution(
    setup: Any, when: str
) -> None:
    request, objects, recognizer = setup
    current = request.identity.deadline_at - timedelta(seconds=5)

    def expire() -> None:
        nonlocal current
        current = request.identity.deadline_at

    if when == "before-input":
        expire()
    if when == "during-input":
        objects.on_open = expire
    if when == "after-dispatch":
        recognizer.after_call = expire
    with pytest.raises(AsrCallError) as error:
        async with consumer(objects, recognizer, now=lambda: current).open(
            request, cancelled=lambda: False
        ) as run:
            _ = [s async for s in run]
    assert error.value.code is AsrFailure.DEADLINE_EXCEEDED
    assert error.value.request_may_have_run == (when == "after-dispatch")
    assert len(recognizer.requests) == (1 if when == "after-dispatch" else 0)
    assert all(s.closed for s in objects.streams)
    if when == "before-input":
        assert not objects.reads


@pytest.mark.parametrize("after_dispatch", [False, True])
async def test_binding_drift_cannot_retarget_execution(setup: Any, after_dispatch: bool) -> None:
    request, objects, recognizer = setup

    def change() -> None:
        recognizer.binding_revision_id = "changed-binding"

    if after_dispatch:
        recognizer.after_call = change
    else:
        objects.on_open = change
    with pytest.raises(AsrCallError) as error:
        async with consumer(objects, recognizer).open(request, cancelled=lambda: False) as run:
            _ = [s async for s in run]
    assert error.value.code is AsrFailure.RESPONSE_INVALID
    assert error.value.request_may_have_run == after_dispatch
    assert len(recognizer.requests) == (1 if after_dispatch else 0)
    assert all(s.closed for s in objects.streams)


async def test_cancellation_after_engine_call_keeps_execution_ambiguous(setup: Any) -> None:
    request, objects, recognizer = setup
    cancelled = False

    def cancel() -> None:
        nonlocal cancelled
        cancelled = True

    recognizer.after_call = cancel
    with pytest.raises(AsrCallError) as error:
        async with consumer(objects, recognizer).open(request, cancelled=lambda: cancelled) as run:
            _ = [s async for s in run]
    assert error.value.code is AsrFailure.CANCELLED
    assert error.value.request_may_have_run and len(recognizer.requests) == 1
    assert all(s.closed for s in objects.streams)


async def test_segment_final_deadline_rejects_a_late_engine_result(setup: Any) -> None:
    request, objects, recognizer = setup
    current = 50.0

    def delay_result() -> None:
        nonlocal current
        current += 0.8

    recognizer.after_call = delay_result
    with pytest.raises(AsrCallError) as error:
        async with consumer(objects, recognizer, monotonic=lambda: current).open(
            request, cancelled=lambda: False
        ) as run:
            _ = [s async for s in run]
    assert error.value.code is AsrFailure.DEADLINE_EXCEEDED
    assert error.value.request_may_have_run and len(recognizer.requests) == 1
    assert all(s.closed for s in objects.streams)


async def test_pending_segment_does_not_get_a_new_deadline_after_consumer_wait(setup: Any) -> None:
    request, objects, recognizer = setup
    current = 50.0
    async with consumer(objects, recognizer, monotonic=lambda: current).open(
        request, cancelled=lambda: False
    ) as run:
        first = await run.__anext__()
        assert not first.no_speech and len(recognizer.requests) == 1
        current += 0.8
        with pytest.raises(AsrCallError) as error:
            await run.__anext__()
        assert error.value.code is AsrFailure.DEADLINE_EXCEEDED
        assert error.value.request_may_have_run
        assert len(recognizer.requests) == 1 and run.summary is None
    assert all(s.closed for s in objects.streams)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_completion_tokens": True},
        {"max_completion_tokens": 0},
        {"max_completion_tokens": 4097},
        {"max_call_seconds": 0},
        {"max_call_seconds": float("nan")},
        {"max_call_seconds": 301},
    ],
)
def test_invalid_local_bounds_are_not_wire_defaults(setup: Any, kwargs: Any) -> None:
    _, objects, recognizer = setup
    with pytest.raises(ValueError):
        consumer(objects, recognizer, **kwargs)
    assert not objects.reads and not recognizer.requests
