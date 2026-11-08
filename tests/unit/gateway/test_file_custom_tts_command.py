"""Published clone material drives the Base custom TTS path, never a builtin fallback."""

import asyncio
import copy
import io
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputLimits,
    ExecutionObjectReference,
    VerifiedExecutionObject,
)
from speechnexus_gateway.domain.execution_results import ExecutionAudioStagingResult
from speechnexus_gateway.domain.file_execution import FileCommandInvalidError
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmissionIdentity,
    SubmitRequest,
)
from speechnexus_gateway.domain.tts import (
    BuiltinVoiceBinding,
    TtsAudio,
    TtsDeliveryFormat,
    TtsPolicy,
)
from speechnexus_gateway.domain.tts_results import PreparedTtsResult
from speechnexus_gateway.domain.voice_clone import (
    ReferenceTextMode,
    VoiceCloneCallError,
    VoiceCloneExecutionTarget,
    VoiceCloneFailure,
    VoiceClonePolicy,
    VoiceCloneRequest,
)
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.interfaces.file_tts_command import (
    CommandFileCustomTts,
    CommandFileTts,
)
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
    def __init__(self, *, error: VoiceCloneCallError | None = None) -> None:
        self.binding_revision_id = "binding-voice-clone"
        self.error = error
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
        return result.receipt("sn-custom-tts", "version-1")


def custom_command(**changes: Any) -> dict[str, Any]:
    command = copy.deepcopy(command_example(0))
    command["resources"].update(
        {
            "voice": {"id": "voice-demo", "revision": "1"},
            "voice_material": {
                "asset_or_artifact_id": "synthetic-reference",
                "workspace_id": "ws-demo",
                "storage_bucket": "synthetic",
                "object_key": "synthetic/reference.wav",
                "content_sha256": "sha256:" + "a" * 64,
                "byte_length": len(PAYLOAD),
            },
            "voice_kind": "custom",
        }
    )
    command["parameters"]["reference_text"] = REFERENCE_TEXT
    command["text"] = "Custom voice narration."
    command.update(changes)
    return command


def custom_request(
    *,
    command: dict[str, Any] | None = None,
    deadline: timedelta = timedelta(hours=1),
    voice_revision_id: str | None = "voice-rev-1",
) -> SubmitRequest:
    value = copy.deepcopy(custom_command() if command is None else command)
    identity = SubmissionIdentity(
        submit_intent_id="intent-custom-tts",
        lease_id="lease-custom-tts",
        operation_id="operation-custom-tts",
        attempt_id="attempt-custom-tts",
        stage_id="stage-custom-tts",
        workspace_id="ws-demo",
        pool_id="voice-creation",
        host_node_id="node-voice-clone",
        gateway_id="gateway-voice-clone",
        instance_id="instance-voice-clone",
        instance_generation=1,
        binding_revision_id="binding-voice-clone",
        policy_revision_id="policy-voice-clone",
        voice_revision_id=voice_revision_id,
        fencing_token=1,
        command_digest=command_digest(value),
        deadline_at=datetime.now(UTC) + deadline,
        max_output_audio_ms=60000,
        sample_rate_hz=24000,
    )
    return SubmitRequest(identity=identity, command=value, output_stream_id="stream-custom-tts")


def build(
    *,
    objects: Objects | None = None,
    synthesizer: Synthesizer | None = None,
    policy: VoiceClonePolicy = BINDING,
) -> tuple[CommandFileCustomTts, Objects, Synthesizer]:
    store_objects = objects or Objects()
    engine = synthesizer or Synthesizer()
    return (
        CommandFileCustomTts(
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


async def test_published_material_drives_one_base_synthesis_and_stages_once() -> None:
    runner, objects, engine = build()
    store = Store()
    request = custom_request()

    outcome = await runner.stage_result(request, store=store, cancelled=lambda: False)

    assert len(objects.reads) == 1
    assert objects.reads[0].asset_or_artifact_id == "synthetic-reference"
    assert len(engine.calls) == 1
    frozen, policy = engine.calls[0]
    assert frozen.preview_text == "Custom voice narration."
    assert frozen.reference_text == REFERENCE_TEXT
    assert frozen.mode is ReferenceTextMode.ICL
    assert frozen.reference_audio.duration_ms == 1500
    assert policy.config_revision == "policy-voice-clone"
    assert outcome.terminal is not None
    assert outcome.terminal.response_ids == ("intent-custom-tts",)
    assert len(store.results) == 1
    assert engine.targets[-1] == TARGET


async def test_missing_voice_material_is_refused_before_reading_or_dispatch() -> None:
    command = custom_command()
    command["resources"]["voice_material"] = None
    runner, objects, engine = build()

    with pytest.raises(FileCommandInvalidError):
        await runner.stage_result(
            custom_request(command=command), store=Store(), cancelled=lambda: False
        )

    assert objects.reads == []
    assert engine.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"reference_text": None},
        {"reference_text": ""},
        {"reference_text": "x" * 10001},
    ],
)
async def test_missing_or_oversized_reference_text_is_refused(change: dict[str, Any]) -> None:
    command = custom_command()
    command["parameters"].update(change)
    runner, objects, engine = build()

    with pytest.raises(FileCommandInvalidError):
        await runner.stage_result(
            custom_request(command=command), store=Store(), cancelled=lambda: False
        )

    assert objects.reads == []
    assert engine.calls == []


async def test_reference_text_density_is_checked_against_the_clip() -> None:
    command = custom_command()
    command["parameters"]["reference_text"] = "x" * 1000  # 1.5 s cannot hold this
    runner, objects, engine = build()

    with pytest.raises(FileCommandInvalidError):
        await runner.stage_result(
            custom_request(command=command), store=Store(), cancelled=lambda: False
        )

    assert len(objects.reads) == 1
    assert engine.calls == []


async def test_a_builtin_command_is_refused_and_never_falls_back() -> None:
    runner, objects, engine = build()

    with pytest.raises(FileCommandInvalidError):
        await runner.stage_result(
            custom_request(command=command_example(0)), store=Store(), cancelled=lambda: False
        )

    assert objects.reads == []
    assert engine.calls == []


async def test_the_builtin_consumer_still_refuses_custom_material() -> None:
    builtin = CommandFileTts(
        synthesizer=cast_synthesizer(),
        encoder=LocalTtsEncoder(),
        binding_policy=TtsPolicy("policy-voice-clone", "Vivian"),
        builtin_voice_binding=BuiltinVoiceBinding("voice-demo", "1", "voice-rev-1", "Vivian"),
        max_payload_bytes=1024 * 1024,
    )

    with pytest.raises(InvalidExecutionRequestError):
        builtin.validate_request(custom_request())


def cast_synthesizer() -> Any:
    """The builtin consumer constructor only needs a binding revision; validation never calls it."""

    class Unused:
        binding_revision_id = "binding-voice-clone"

        def validate_execution_target(self, target: object) -> None:
            raise AssertionError("validation must not inspect the engine target")

        async def synthesize(self, *args: object, **kwargs: object) -> TtsAudio:
            raise AssertionError("validation must not dispatch")

    return Unused()


async def test_an_engine_failure_is_reported_without_a_staged_result() -> None:
    runner, _, engine = build(
        synthesizer=Synthesizer(
            error=VoiceCloneCallError(
                VoiceCloneFailure.TRANSPORT_UNKNOWN, request_may_have_run=True
            )
        )
    )
    store = Store()

    with pytest.raises(VoiceCloneCallError):
        await runner.stage_result(custom_request(), store=store, cancelled=lambda: False)

    assert len(engine.calls) == 1
    assert store.results == []
    assert TtsDeliveryFormat.WAV.value == "wav"


def test_a_missing_pinned_voice_revision_is_refused() -> None:
    runner, _, _ = build()
    with pytest.raises(FileCommandInvalidError):
        runner.validate_request(custom_request(voice_revision_id=None))


def test_cancellation_before_dispatch_never_reads_material() -> None:
    runner, objects, engine = build()

    async def scenario() -> None:
        with pytest.raises(VoiceCloneCallError):
            await runner.synthesize(custom_request(), cancelled=lambda: True)

    asyncio.run(scenario())
    assert objects.reads == []
    assert engine.calls == []
