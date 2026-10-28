"""Frozen file TTS commands drive one bounded synthesis and optional private staging."""

import copy
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.execution_results import ExecutionAudioStagingResult
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmissionIdentity,
    SubmitRequest,
)
from speechnexus_gateway.domain.tts import (
    TtsAudio,
    TtsCallError,
    TtsDelivery,
    TtsDeliveryFormat,
    TtsExecutionTarget,
    TtsFailure,
    TtsPolicy,
    TtsRequest,
)
from speechnexus_gateway.domain.tts_results import PreparedTtsResult
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.interfaces.file_tts_command import CommandFileTts
from tests.fake.execution_command import command_example

PCM = b"\x01\x00" * 2400
BINDING = TtsPolicy("policy-tts", "Vivian")


class Synthesizer:
    def __init__(
        self,
        *,
        error: TtsCallError | None = None,
        target: TtsExecutionTarget | None = None,
    ) -> None:
        self.binding_revision_id = "binding-tts"
        self.error = error
        self.target = target or TtsExecutionTarget(
            "node-tts", "gateway-tts", "tts-batch", "instance-tts", 1, "binding-tts"
        )
        self.calls: list[tuple[TtsRequest, TtsPolicy]] = []

    def validate_execution_target(self, target: TtsExecutionTarget) -> None:
        if target != self.target:
            raise ValueError("synthetic target mismatch")

    async def synthesize(
        self,
        request: TtsRequest,
        policy: TtsPolicy,
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


class Encoder:
    def __init__(self) -> None:
        self.calls: list[tuple[TtsDeliveryFormat, int]] = []

    def encode(
        self,
        audio: TtsAudio,
        *,
        target: TtsDeliveryFormat,
        max_payload_bytes: int,
        cancelled: Callable[[], bool],
    ) -> TtsDelivery:
        self.calls.append((target, max_payload_bytes))
        return LocalTtsEncoder().encode(
            audio, target=target, max_payload_bytes=max_payload_bytes, cancelled=cancelled
        )


class Store:
    def __init__(self) -> None:
        self.results: list[PreparedTtsResult] = []

    async def stage(
        self, result: PreparedTtsResult, *, cancelled: Callable[[], bool]
    ) -> ExecutionAudioStagingResult:
        assert not cancelled()
        self.results.append(result)
        return result.receipt("sn-tts", "version-1")


def tts_request(
    *,
    command: dict[str, Any] | None = None,
    deadline: timedelta = timedelta(hours=1),
    voice_revision_id: str | None = "voice-rev-1",
) -> SubmitRequest:
    value = copy.deepcopy(command_example(0) if command is None else command)
    return SubmitRequest(
        identity=SubmissionIdentity(
            submit_intent_id="intent-tts",
            lease_id="lease-tts",
            operation_id="operation-tts",
            attempt_id="attempt-tts",
            stage_id="stage-tts",
            workspace_id="ws-demo",
            pool_id="tts-batch",
            host_node_id="node-tts",
            gateway_id="gateway-tts",
            instance_id="instance-tts",
            instance_generation=1,
            binding_revision_id="binding-tts",
            policy_revision_id="policy-tts",
            voice_revision_id=voice_revision_id,
            fencing_token=1,
            command_digest=command_digest(value),
            deadline_at=datetime.now(UTC).replace(microsecond=0) + deadline,
            max_output_audio_ms=60000,
            sample_rate_hz=24000,
        ),
        command=value,
        output_stream_id="stream-tts",
    )


def build(
    *,
    now: Callable[[], datetime] | None = None,
    synthesizer: Synthesizer | None = None,
) -> tuple[CommandFileTts, Synthesizer, Encoder]:
    engine = synthesizer or Synthesizer()
    encoder = Encoder()
    return (
        CommandFileTts(
            synthesizer=engine,
            encoder=encoder,
            binding_policy=BINDING,
            max_payload_bytes=1024 * 1024,
            **({} if now is None else {"now": now}),
        ),
        engine,
        encoder,
    )


async def test_supported_command_is_synthesized_once_with_frozen_parameters() -> None:
    runner, engine, encoder = build()
    result = await runner.synthesize(tts_request(), cancelled=lambda: False)

    assert len(engine.calls) == 1
    frozen, policy = engine.calls[0]
    assert frozen.text == "Synthetic narration."
    assert frozen.request_id == "intent-tts"
    assert policy.voice == "Vivian"
    assert policy.language == "en"
    assert policy.seed == 7
    assert policy.instructions == ""
    assert policy.speed == 1.0
    assert result.execution_id == "intent-tts"
    assert result.delivery.format is TtsDeliveryFormat.WAV
    assert result.duration_ms == 100
    assert encoder.calls == [(TtsDeliveryFormat.WAV, 1024 * 1024)]


async def test_pcm_delivery_format_is_followed() -> None:
    command = command_example(0)
    command["parameters"]["response_format"] = "pcm"
    runner, _, encoder = build()
    result = await runner.synthesize(tts_request(command=command), cancelled=lambda: False)
    assert result.delivery.format is TtsDeliveryFormat.PCM
    assert encoder.calls[0][0] is TtsDeliveryFormat.PCM


async def test_private_staging_returns_one_receipt_per_call() -> None:
    runner, engine, _ = build()
    store = Store()
    first = await runner.stage_result(tts_request(), store=store, cancelled=lambda: False)
    second = await runner.stage_result(tts_request(), store=store, cancelled=lambda: False)

    assert len(engine.calls) == 2
    assert len(store.results) == 2
    assert store.results[0].workspace_id == "ws-demo"
    assert store.results[0].request_digest.startswith("sha256:")
    assert first.result.storage_version == "version-1"
    assert second.result.reference.object_key.endswith(".wav")
    assert first.terminal is not None
    assert first.terminal.response_ids == (first.terminal.submit_intent_id,)


async def test_expired_deadline_refuses_before_dispatch() -> None:
    runner, engine, _ = build(now=lambda: datetime.now(UTC) + timedelta(hours=2))
    with pytest.raises(TtsCallError) as error:
        await runner.synthesize(tts_request(), cancelled=lambda: False)
    assert error.value.code is TtsFailure.DEADLINE_EXCEEDED
    assert error.value.request_may_have_run is False
    assert engine.calls == []


async def test_cancellation_before_dispatch_never_calls_the_engine() -> None:
    runner, engine, _ = build()
    with pytest.raises(TtsCallError) as error:
        await runner.synthesize(tts_request(), cancelled=lambda: True)
    assert error.value.code is TtsFailure.CANCELLED
    assert engine.calls == []


def test_validate_request_freezes_a_copy_without_io() -> None:
    command = command_example(0)
    request = tts_request(command=command)
    runner, engine, encoder = build()
    frozen = runner.validate_request(request)
    command["text"] = "mutated after preparation"
    assert frozen.command != command
    assert engine.calls == []
    assert encoder.calls == []


def _mutated(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    command = command_example(0)
    target: dict[str, Any] = command
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return command


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("task_kind",), "tts_realtime"),
        (("stage_role",), "tts_talker"),
        (("parameters", "audio_delivery"), "incremental"),
        (("parameters", "text_commit_mode"), "server"),
        (("parameters", "pronunciation_revision"), "pron-1"),
        (("parameters", "response_format"), "flac"),
        (("parameters", "response_format"), "opus"),
        (("resources", "voice_kind"), "clone"),
        (("resources", "voice_material"), {"id": "material", "revision": "1"}),
        (("resources", "vocabulary"), [{"id": "vocab", "revision": "1"}]),
        (("parameters", "planner", "max_chars"), 4),
    ],
)
def test_unsupported_capabilities_are_rejected(path: tuple[str, ...], value: Any) -> None:
    runner, engine, _ = build()
    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(tts_request(command=_mutated(path, value)))
    assert engine.calls == []


def test_streamed_text_is_rejected() -> None:
    command = command_example(0)
    command["text"] = None
    command["text_stream"] = {
        "storage": {
            "asset_or_artifact_id": "asset-text",
            "workspace_id": "ws-demo",
            "storage_bucket": "synthetic",
            "object_key": "synthetic/text.txt",
            "content_sha256": "sha256:" + "a" * 64,
            "byte_length": 10,
        }
    }
    runner, _, _ = build()
    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(tts_request(command=command))


def test_text_longer_than_one_declared_segment_is_rejected() -> None:
    command = command_example(0)
    command["text"] = "x" * 201
    runner, _, _ = build()
    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(tts_request(command=command))


def test_command_requires_a_pinned_voice_revision() -> None:
    runner, _, _ = build()
    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(tts_request(voice_revision_id=None))


def test_tampered_command_is_rejected() -> None:
    request = tts_request()
    assert isinstance(request.command, dict)
    request.command["text"] = "changed after admission"
    runner, _, _ = build()
    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(request)


def test_identity_mismatches_are_rejected() -> None:
    runner, _, _ = build()
    request = tts_request()
    for tampered in (
        replace(request, identity=replace(request.identity, binding_revision_id="other-binding")),
        replace(request, identity=replace(request.identity, policy_revision_id="other-policy")),
    ):
        with pytest.raises(InvalidExecutionRequestError):
            runner.validate_request(tampered)


def test_audio_identity_is_required() -> None:
    runner, engine, _ = build()
    request = tts_request()
    tampered = replace(
        request,
        identity=replace(request.identity, max_output_audio_ms=None, sample_rate_hz=None),
        output_stream_id=None,
    )
    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(tampered)
    assert engine.calls == []


def test_audio_clock_and_ceiling_must_match_the_binding() -> None:
    runner, engine, _ = build()
    request = tts_request()
    for tampered in (
        replace(
            request,
            identity=replace(request.identity, sample_rate_hz=16000),
        ),
        replace(
            request,
            identity=replace(request.identity, max_output_audio_ms=600001),
        ),
    ):
        with pytest.raises(InvalidExecutionRequestError):
            runner.validate_request(tampered)
    assert engine.calls == []


def test_admitted_target_must_match_the_configured_generation() -> None:
    engine = Synthesizer(
        target=TtsExecutionTarget(
            "node-other", "gateway-tts", "tts-batch", "instance-tts", 1, "binding-tts"
        )
    )
    runner, _, _ = build(synthesizer=engine)
    with pytest.raises(InvalidExecutionRequestError):
        runner.validate_request(tts_request())


def _with_ceiling(request: SubmitRequest, ceiling_ms: int) -> SubmitRequest:
    return replace(request, identity=replace(request.identity, max_output_audio_ms=ceiling_ms))


async def test_request_ceiling_tightens_the_effective_policy() -> None:
    runner, engine, _ = build()
    await runner.synthesize(_with_ceiling(tts_request(), 100), cancelled=lambda: False)

    _, policy = engine.calls[0]
    assert policy.max_audio_ms == 100
    assert policy.max_pcm_bytes == 4800


async def test_request_ceiling_at_binding_keeps_the_binding_pcm_ceiling() -> None:
    runner, engine, _ = build()
    await runner.synthesize(_with_ceiling(tts_request(), 600000), cancelled=lambda: False)

    _, policy = engine.calls[0]
    assert policy.max_audio_ms == 600000
    assert policy.max_pcm_bytes == BINDING.max_pcm_bytes


class LongSynthesizer(Synthesizer):
    """Returns 120 ms of PCM no matter what the request ceiling allowed."""

    async def synthesize(
        self, request: TtsRequest, policy: TtsPolicy, *, cancelled: Callable[[], bool]
    ) -> TtsAudio:
        assert not cancelled()
        self.calls.append((request, policy))
        return TtsAudio(
            request.request_id,
            "audio/wav",
            b"\x00\x00" * 2880,
            policy.sample_rate_hz,
            1,
            policy.digest,
        )


async def test_audio_over_the_request_ceiling_is_not_published() -> None:
    engine = LongSynthesizer()
    runner, _, _ = build(synthesizer=engine)
    store = Store()
    with pytest.raises(TtsCallError) as error:
        await runner.stage_result(
            _with_ceiling(tts_request(), 100), store=store, cancelled=lambda: False
        )

    assert error.value.code is TtsFailure.RESPONSE_INVALID
    _, policy = engine.calls[0]
    assert policy.max_audio_ms == 100
    assert policy.max_pcm_bytes == 4800
    assert store.results == []
