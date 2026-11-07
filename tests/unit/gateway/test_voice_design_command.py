"""Frozen VoiceDesign commands drive one bounded design and optional private staging."""

import asyncio
import copy
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.execution_results import ExecutionAudioStagingResult
from speechnexus_gateway.domain.file_execution import EngineRequestRejectedError
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmissionIdentity,
    SubmitRequest,
)
from speechnexus_gateway.domain.tts import TtsAudio, TtsDeliveryFormat
from speechnexus_gateway.domain.tts_results import PreparedTtsResult
from speechnexus_gateway.domain.voice_design import (
    VoiceDesignCallError,
    VoiceDesignExecutionTarget,
    VoiceDesignFailure,
    VoiceDesignPolicy,
    VoiceDesignRejectedError,
    VoiceDesignRequest,
)
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.interfaces.file_voice_design_command import CommandFileVoiceDesign
from tests.fake.execution_command import command_example

PCM = b"\x01\x00" * 2400
BINDING = VoiceDesignPolicy("policy-voice-design")
TARGET = VoiceDesignExecutionTarget(
    "node-voice-design",
    "gateway-voice-design",
    "voice-creation",
    "instance-voice-design",
    1,
    "binding-voice-design",
)


class Synthesizer:
    def __init__(self, *, error: VoiceDesignCallError | None = None, delay: float = 0.0) -> None:
        self.binding_revision_id = "binding-voice-design"
        self.error = error
        self.delay = delay
        self.calls: list[tuple[VoiceDesignRequest, VoiceDesignPolicy]] = []
        self.targets: list[VoiceDesignExecutionTarget] = []

    def validate_execution_target(self, target: VoiceDesignExecutionTarget) -> None:
        self.targets.append(target)
        if target != TARGET:
            raise ValueError("synthetic target mismatch")

    async def synthesize(
        self,
        request: VoiceDesignRequest,
        policy: VoiceDesignPolicy,
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
        return result.receipt("sn-voice-design", "version-1")


def design_request(
    *,
    command: dict[str, Any] | None = None,
    deadline: timedelta = timedelta(hours=1),
    voice_revision_id: str | None = None,
    max_output_audio_ms: int = 60000,
    sample_rate_hz: int = 24000,
    binding_revision_id: str = "binding-voice-design",
    policy_revision_id: str = "policy-voice-design",
) -> SubmitRequest:
    value = copy.deepcopy(command_example(2) if command is None else command)
    identity = SubmissionIdentity(
        submit_intent_id="intent-voice-design",
        lease_id="lease-voice-design",
        operation_id="operation-voice-design",
        attempt_id="attempt-voice-design",
        stage_id="stage-voice-design",
        workspace_id="ws-demo",
        pool_id="voice-creation",
        host_node_id="node-voice-design",
        gateway_id="gateway-voice-design",
        instance_id="instance-voice-design",
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
        output_stream_id="stream-voice-design",
    )


def build(
    *,
    synthesizer: Synthesizer | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[CommandFileVoiceDesign, Synthesizer]:
    engine = synthesizer or Synthesizer()
    return (
        CommandFileVoiceDesign(
            synthesizer=engine,
            encoder=LocalTtsEncoder(),
            binding_policy=BINDING,
            max_payload_bytes=1024 * 1024,
            **({} if now is None else {"now": now}),
        ),
        engine,
    )


async def test_supported_command_is_frozen_and_designed_once() -> None:
    runner, engine = build()
    result = await runner.synthesize(design_request(), cancelled=lambda: False)

    assert len(engine.calls) == 1
    frozen, policy = engine.calls[0]
    assert frozen.request_id == "intent-voice-design"
    assert frozen.description == command_example(2)["description"]
    assert frozen.preview_text == command_example(2)["preview_text"]
    assert frozen.language == "en"
    assert frozen.seed == 7
    assert policy.config_revision == "policy-voice-design"
    assert policy.task.value == "VoiceDesign"
    assert policy.max_audio_ms == 60000
    assert result.execution_id == "intent-voice-design"
    assert result.sample_rate_hz == 24000
    assert result.delivery.format is TtsDeliveryFormat.WAV
    assert engine.targets[-1] == TARGET


async def test_declared_output_duration_tightens_the_binding() -> None:
    runner, engine = build()
    await runner.synthesize(design_request(max_output_audio_ms=5000), cancelled=lambda: False)
    _, policy = engine.calls[0]
    assert policy.max_audio_ms == 5000
    assert policy.max_pcm_bytes == 5000 * 24000 // 1000 * 2


async def test_a_missing_seed_is_preserved_as_absent() -> None:
    command = command_example(2)
    command["seed"] = None
    runner, engine = build()
    await runner.synthesize(design_request(command=command), cancelled=lambda: False)
    assert engine.calls[0][0].seed is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: replace(request, identity=replace(request.identity, pool_id="tts-batch")),
        lambda request: replace(
            request, identity=replace(request.identity, binding_revision_id="binding-tts")
        ),
        lambda request: replace(
            request, identity=replace(request.identity, policy_revision_id="policy-tts")
        ),
        lambda request: replace(
            request, identity=replace(request.identity, voice_revision_id="voice-rev-1")
        ),
        lambda request: replace(request, identity=replace(request.identity, sample_rate_hz=16000)),
        lambda request: replace(
            request, identity=replace(request.identity, max_output_audio_ms=120000)
        ),
        lambda request: replace(request, command={**request.command, "kind": "tts"}),
        lambda request: replace(
            request,
            command={
                **request.command,
                "resources": {**request.command["resources"], "voice": "v"},
            },
        ),
        lambda request: replace(
            request, command={**request.command, "command_digest": "sha256:" + "0" * 64}
        ),
    ],
)
async def test_unsupported_or_mismatched_commands_are_refused_before_the_engine(
    mutate: Callable[[SubmitRequest], SubmitRequest],
) -> None:
    runner, engine = build()
    request = mutate(design_request())
    with pytest.raises(InvalidExecutionRequestError):
        await runner.synthesize(request, cancelled=lambda: False)
    assert engine.calls == []


def test_validate_request_freezes_without_calling_the_engine() -> None:
    runner, engine = build()
    request = design_request()
    before = copy.deepcopy(request.command)
    frozen = runner.validate_request(request)
    assert frozen.command == before
    assert engine.calls == []


async def test_private_staging_returns_one_receipt_per_call() -> None:
    runner, _ = build()
    store = Store()
    first = await runner.stage_result(design_request(), store=store, cancelled=lambda: False)
    second = await runner.stage_result(design_request(), store=store, cancelled=lambda: False)

    assert len(store.results) == 2
    assert first.result.reference.object_key != second.result.reference.object_key
    assert first.terminal is not None and second.terminal is not None
    assert first.terminal.response_ids == ("intent-voice-design",)
    assert first.terminal.submit_intent_id == "intent-voice-design"
    assert store.results[0].audio.source_pcm_samples == len(PCM) // 2


async def test_an_expired_deadline_never_calls_the_engine() -> None:
    runner, engine = build()
    with pytest.raises(VoiceDesignCallError) as raised:
        await runner.synthesize(
            design_request(deadline=timedelta(seconds=-1)), cancelled=lambda: False
        )
    assert raised.value.code is VoiceDesignFailure.DEADLINE_EXCEEDED
    assert raised.value.request_may_have_run is False
    assert engine.calls == []


async def test_a_slow_engine_times_out_as_an_unknown_claim() -> None:
    runner, engine = build(synthesizer=Synthesizer(delay=5))
    with pytest.raises(VoiceDesignCallError) as raised:
        await runner.synthesize(
            design_request(deadline=timedelta(milliseconds=50)), cancelled=lambda: False
        )
    assert raised.value.code is VoiceDesignFailure.DEADLINE_EXCEEDED
    assert raised.value.request_may_have_run is True
    assert len(engine.calls) == 1


async def test_engine_rejection_is_not_a_timeout_and_never_stages() -> None:
    runner, _ = build(synthesizer=Synthesizer(error=VoiceDesignRejectedError()))
    store = Store()
    with pytest.raises(VoiceDesignRejectedError) as raised:
        await runner.stage_result(design_request(), store=store, cancelled=lambda: False)

    assert isinstance(raised.value, EngineRequestRejectedError)
    assert raised.value.code is VoiceDesignFailure.ENGINE_REJECTED
    assert raised.value.request_may_have_run is False
    assert store.results == []


async def test_a_real_engine_client_error_propagates_as_a_proven_rejection() -> None:
    from speechnexus_gateway.infrastructure.vllm_voice_design import (
        VllmVoiceDesignEndpoint,
        VllmVoiceDesignSynthesizer,
    )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(400, headers={"content-type": "application/json"})

    endpoint = VllmVoiceDesignEndpoint(
        "127.0.0.1",
        8002,
        "synthetic-voice-design",
        "binding-voice-design",
        "a" * 64,
        execution_target=TARGET,
    )
    runner = CommandFileVoiceDesign(
        synthesizer=VllmVoiceDesignSynthesizer(endpoint, transport=httpx.MockTransport(handler)),
        encoder=LocalTtsEncoder(),
        binding_policy=BINDING,
        max_payload_bytes=1024 * 1024,
    )
    store = Store()
    with pytest.raises(VoiceDesignRejectedError) as raised:
        await runner.stage_result(design_request(), store=store, cancelled=lambda: False)

    assert isinstance(raised.value, EngineRequestRejectedError)
    assert raised.value.request_may_have_run is False
    assert len(requests) == 1
    assert store.results == []
