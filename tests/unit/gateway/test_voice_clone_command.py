"""Frozen Base clone commands drive one bounded clone and optional private staging."""

import asyncio
import base64
import copy
import io
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputLimits,
    ExecutionObjectReference,
    VerifiedExecutionObject,
)
from speechnexus_gateway.domain.execution_results import ExecutionAudioStagingResult
from speechnexus_gateway.domain.file_execution import (
    EngineRequestRejectedError,
    FileCommandInvalidError,
)
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmissionIdentity,
    SubmitRequest,
)
from speechnexus_gateway.domain.tts import TtsAudio
from speechnexus_gateway.domain.tts_results import PreparedTtsResult
from speechnexus_gateway.domain.voice_clone import (
    ReferenceTextMode,
    VoiceCloneCallError,
    VoiceCloneExecutionTarget,
    VoiceCloneFailure,
    VoiceClonePolicy,
    VoiceCloneRejectedError,
    VoiceCloneRequest,
)
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.interfaces.file_voice_clone_command import CommandFileVoiceClone
from tests.fake.execution_command import command_example
from tests.fake.file_voice_clone_configuration import REFERENCE_TEXT, reference_wav

PCM = b"\x01\x00" * 2400
BINDING = VoiceClonePolicy("policy-voice-clone")
TARGET = VoiceCloneExecutionTarget(
    "node-voice-clone",
    "gateway-voice-clone",
    "voice-creation",
    "instance-voice-clone",
    1,
    "binding-voice-clone",
)
PAYLOAD = reference_wav()


class Objects:
    def __init__(self, payload: bytes = PAYLOAD) -> None:
        self.payload = payload
        self.reads: list[ExecutionObjectReference] = []

    @asynccontextmanager
    async def open_verified(
        self,
        reference: ExecutionObjectReference,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> AsyncIterator[VerifiedExecutionObject]:
        assert not cancelled()
        self.reads.append(reference)
        with io.BytesIO(self.payload) as stream:
            yield VerifiedExecutionObject(reference, stream, "synthetic-version")


class Synthesizer:
    def __init__(self, *, error: VoiceCloneCallError | None = None, delay: float = 0.0) -> None:
        self.binding_revision_id = "binding-voice-clone"
        self.error = error
        self.delay = delay
        self.calls: list[tuple[VoiceCloneRequest, VoiceClonePolicy]] = []
        self.targets: list[VoiceCloneExecutionTarget] = []

    def validate_execution_target(self, target: VoiceCloneExecutionTarget) -> None:
        self.targets.append(target)
        if target != TARGET:
            raise ValueError("synthetic target mismatch")

    async def synthesize(
        self,
        request: VoiceCloneRequest,
        policy: VoiceClonePolicy,
        *,
        cancelled: Callable[[], bool],
    ) -> TtsAudio:
        assert not cancelled()
        self.calls.append((request, policy))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return TtsAudio(
            request.request_id, "audio/wav", PCM, policy.sample_rate_hz, 1, policy.digest
        )

    async def warmup(self) -> None:
        raise AssertionError("the command never performs readiness synthesis")


class Store:
    def __init__(self) -> None:
        self.results: list[PreparedTtsResult] = []

    async def stage(
        self, result: PreparedTtsResult, *, cancelled: Callable[[], bool]
    ) -> ExecutionAudioStagingResult:
        assert not cancelled()
        self.results.append(result)
        return result.receipt("sn-voice-clone", "version-1")


def clone_request(
    *,
    command: dict[str, Any] | None = None,
    payload: bytes = PAYLOAD,
    deadline: timedelta = timedelta(hours=1),
    voice_revision_id: str | None = None,
    max_output_audio_ms: int = 60000,
    sample_rate_hz: int = 24000,
    binding_revision_id: str = "binding-voice-clone",
    policy_revision_id: str = "policy-voice-clone",
) -> SubmitRequest:
    value = copy.deepcopy(command_example(3) if command is None else command)
    if isinstance(value.get("reference_audio"), dict):
        value["reference_audio"]["storage"].update(
            {
                "byte_length": len(payload),
                "content_sha256": "sha256:" + "a" * 64,
            }
        )
    identity = SubmissionIdentity(
        submit_intent_id="intent-voice-clone",
        lease_id="lease-voice-clone",
        operation_id="operation-voice-clone",
        attempt_id="attempt-voice-clone",
        stage_id="stage-voice-clone",
        workspace_id="ws-demo",
        pool_id="voice-creation",
        host_node_id="node-voice-clone",
        gateway_id="gateway-voice-clone",
        instance_id="instance-voice-clone",
        instance_generation=1,
        binding_revision_id=binding_revision_id,
        policy_revision_id=policy_revision_id,
        voice_revision_id=voice_revision_id,
        fencing_token=1,
        command_digest=command_digest(value),
        deadline_at=datetime.now(UTC) + deadline,
        max_output_audio_ms=max_output_audio_ms,
        sample_rate_hz=sample_rate_hz,
    )
    return SubmitRequest(
        identity=identity,
        command=value,
        output_stream_id="stream-voice-clone",
    )


def prepared_command(**changes: Any) -> dict[str, Any]:
    command = copy.deepcopy(command_example(3))
    command["preview_text"] = "试听文本。"
    command["reference_text"] = REFERENCE_TEXT
    command["reference_audio"].update({"sample_rate_hz": 16000, "channels": 1})
    command.update(changes)
    return command


def build(
    *,
    objects: Objects | None = None,
    synthesizer: Synthesizer | None = None,
    policy: VoiceClonePolicy = BINDING,
) -> tuple[CommandFileVoiceClone, Objects, Synthesizer]:
    store_objects = objects or Objects()
    engine = synthesizer or Synthesizer()
    return (
        CommandFileVoiceClone(
            inputs=FileExecutionInputs(store_objects),
            synthesizer=engine,
            encoder=LocalTtsEncoder(),
            binding_policy=policy,
            input_limits=ExecutionInputLimits(8 * 1024 * 1024, 1024 * 1024, 5),
            max_payload_bytes=1024 * 1024,
        ),
        store_objects,
        engine,
    )


async def test_supported_command_reads_one_reference_and_stages_once() -> None:
    runner, objects, engine = build()
    store = Store()
    request = clone_request(command=prepared_command())
    outcome = await runner.stage_result(request, store=store, cancelled=lambda: False)

    assert len(objects.reads) == 1
    assert len(engine.calls) == 1
    frozen, policy = engine.calls[0]
    assert frozen.request_id == "intent-voice-clone"
    assert frozen.mode is ReferenceTextMode.ICL
    assert frozen.reference_text == REFERENCE_TEXT
    assert frozen.reference_audio.duration_ms == 1500
    assert frozen.language == "en"
    assert policy.config_revision == "policy-voice-clone"
    assert outcome.terminal is not None
    assert outcome.terminal.response_ids == ("intent-voice-clone",)
    assert len(store.results) == 1
    assert engine.targets[-1] == TARGET


@pytest.mark.parametrize("index", [0, 1, 2, 7])
async def test_every_other_command_kind_is_refused_before_reading_or_dispatch(index: int) -> None:
    runner, objects, engine = build()
    with pytest.raises(InvalidExecutionRequestError):
        await runner.stage_result(
            clone_request(command=command_example(index)), store=Store(), cancelled=lambda: False
        )
    assert objects.reads == []
    assert engine.calls == []


async def test_the_auto_language_reaches_the_engine_as_auto() -> None:
    runner, _, engine = build()
    request = clone_request(command=prepared_command(language="auto"))

    await runner.stage_result(request, store=Store(), cancelled=lambda: False)

    assert engine.calls[0][0].language == "auto"


async def test_a_missing_transcript_is_refused_before_reading_or_dispatch() -> None:
    command = prepared_command(reference_text=None)
    runner, objects, engine = build()
    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(clone_request(command=command))
    assert objects.reads == []
    assert engine.calls == []


async def test_a_natural_pairing_tempo_is_accepted() -> None:
    payload = reference_wav(duration_ms=2500)
    command = prepared_command(reference_text="x" * 51)
    runner, _, engine = build(objects=Objects(payload))
    await runner.stage_result(
        clone_request(command=command, payload=payload), store=Store(), cancelled=lambda: False
    )
    assert len(engine.calls) == 1
    assert engine.calls[0][0].reference_text == "x" * 51


async def test_a_blank_transcript_is_refused_before_reading_or_dispatch() -> None:
    command = prepared_command(reference_text=" \t")
    runner, objects, engine = build()
    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(clone_request(command=command))
    assert objects.reads == []
    assert engine.calls == []


async def test_x_vector_only_is_used_only_when_the_binding_permits_it() -> None:
    command = prepared_command(reference_text=None)
    runner, _, engine = build(
        policy=VoiceClonePolicy("policy-voice-clone", allow_x_vector_only=True)
    )
    store = Store()
    await runner.stage_result(clone_request(command=command), store=store, cancelled=lambda: False)

    assert engine.calls[0][0].mode is ReferenceTextMode.X_VECTOR_ONLY
    assert engine.calls[0][0].reference_text is None
    assert len(store.results) == 1


async def test_an_oversized_declared_reference_is_refused_before_reading() -> None:
    command = prepared_command()
    runner, objects, engine = build()
    request = clone_request(command=command, payload=PAYLOAD)
    cast(dict[str, Any], request.command)["reference_audio"]["storage"].update(
        {"byte_length": BINDING.max_reference_audio_bytes + 1}
    )
    request = replace(
        request, identity=replace(request.identity, command_digest=command_digest(request.command))
    )

    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(request)
    assert objects.reads == []
    assert engine.calls == []


async def test_a_reference_that_is_not_an_approved_wav_is_refused_before_the_engine() -> None:
    payload = b"not a wave file" * 4
    command = prepared_command()
    runner, objects, engine = build(objects=Objects(payload))
    with pytest.raises(FileCommandInvalidError):
        await runner.stage_result(
            clone_request(command=command, payload=payload), store=Store(), cancelled=lambda: False
        )
    assert len(objects.reads) == 1
    assert engine.calls == []


async def test_a_transcript_that_cannot_match_the_clip_is_refused_before_the_engine() -> None:
    command = prepared_command(reference_text="x" * 40)
    runner, _, engine = build()
    with pytest.raises(InvalidExecutionRequestError):
        await runner.stage_result(
            clone_request(command=command), store=Store(), cancelled=lambda: False
        )
    assert engine.calls == []


async def test_a_declared_clock_that_contradicts_the_clip_is_refused() -> None:
    command = prepared_command()
    command["reference_audio"]["sample_rate_hz"] = 24000
    runner, _, engine = build()
    with pytest.raises(InvalidExecutionRequestError):
        await runner.stage_result(
            clone_request(command=command), store=Store(), cancelled=lambda: False
        )
    assert engine.calls == []


async def test_an_expired_deadline_never_reads_or_calls() -> None:
    runner, objects, engine = build()
    with pytest.raises(VoiceCloneCallError) as raised:
        await runner.stage_result(
            clone_request(command=prepared_command(), deadline=timedelta(seconds=-1)),
            store=Store(),
            cancelled=lambda: False,
        )
    assert raised.value.code is VoiceCloneFailure.DEADLINE_EXCEEDED
    assert raised.value.request_may_have_run is False
    assert objects.reads == []
    assert engine.calls == []


async def test_a_slow_engine_times_out_as_an_unknown_claim() -> None:
    runner, _, engine = build(synthesizer=Synthesizer(delay=5))
    with pytest.raises(VoiceCloneCallError) as raised:
        await runner.stage_result(
            clone_request(command=prepared_command(), deadline=timedelta(milliseconds=50)),
            store=Store(),
            cancelled=lambda: False,
        )
    assert raised.value.code is VoiceCloneFailure.DEADLINE_EXCEEDED
    assert raised.value.request_may_have_run is True
    assert len(engine.calls) == 1


async def test_engine_rejection_is_not_a_timeout_and_never_stages() -> None:
    runner, _, engine = build(synthesizer=Synthesizer(error=VoiceCloneRejectedError()))
    store = Store()
    with pytest.raises(VoiceCloneRejectedError) as raised:
        await runner.stage_result(
            clone_request(command=prepared_command()), store=store, cancelled=lambda: False
        )

    assert isinstance(raised.value, EngineRequestRejectedError)
    assert raised.value.code is VoiceCloneFailure.ENGINE_REJECTED
    assert raised.value.request_may_have_run is False
    assert engine.calls and store.results == []


def test_validate_request_freezes_without_reading_or_calling_the_engine() -> None:
    runner, objects, engine = build()
    request = clone_request(command=prepared_command())
    before = copy.deepcopy(request.command)
    frozen = runner.validate_request(request)
    assert frozen.command == before
    assert objects.reads == [] and engine.calls == []


async def test_reference_bytes_never_reach_a_log_or_an_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = b"RIFF" + b"\x00" * 60
    encoded = base64.b64encode(payload).decode("ascii")
    runner, _, _ = build(objects=Objects(payload))
    with caplog.at_level(logging.DEBUG), pytest.raises(InvalidExecutionRequestError) as raised:
        await runner.stage_result(
            clone_request(command=prepared_command(), payload=payload),
            store=Store(),
            cancelled=lambda: False,
        )
    assert encoded not in caplog.text
    assert encoded not in str(raised.value)
    assert "data:audio/wav;base64" not in caplog.text
