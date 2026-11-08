"""Dispatch-time command refusals must be deterministic, never transport unknowns.

Every file lane validates the frozen request before any engine or object call. A
structural refusal there is a fact the gateway proved locally; it must become the
non-retryable ``invalid_execution_request`` terminal and stay separate from the
``engine_transport_unknown`` fallback used for genuinely unknown hand-offs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from speechnexus_gateway.application.command_dispatch import OriginalCommandDispatch
from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputLimits,
    ExecutionObjectReference,
    VerifiedExecutionObject,
)
from speechnexus_gateway.domain.file_execution import FileCommandInvalidError, FileExecutionLimits
from speechnexus_gateway.domain.media import AudioDecodeLimits
from speechnexus_gateway.domain.ports import DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.recovery import MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    StopEvidenceKind,
    SubmissionIdentity,
    SubmitRequest,
)
from speechnexus_gateway.domain.tts import BuiltinVoiceBinding, TtsPolicy
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.domain.voice_clone import VoiceClonePolicy
from speechnexus_gateway.domain.voice_design import VoiceDesignPolicy
from speechnexus_gateway.file_asr_runtime import AsrFileStage
from speechnexus_gateway.file_speech_runtime import _RoutedExecution, _Stores
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.interfaces.file_asr_command import CommandFileAsr
from speechnexus_gateway.interfaces.file_tts_command import CommandFileCustomTts, CommandFileTts
from speechnexus_gateway.interfaces.file_voice_clone_command import CommandFileVoiceClone
from speechnexus_gateway.interfaces.file_voice_design_command import CommandFileVoiceDesign
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress
from tests.fake.execution_command import command_example
from tests.fake.vad import ScriptedVadModel

_NOW = datetime.now(UTC).replace(microsecond=0)
_REFERENCE = {
    "asset_or_artifact_id": "synthetic-reference",
    "workspace_id": "ws-demo",
    "storage_bucket": "synthetic",
    "object_key": "synthetic/reference.wav",
    "content_sha256": "sha256:" + "a" * 64,
    "byte_length": 100,
}


class _Objects:
    """No dispatch-time refusal may read an input object."""

    async def open_verified(
        self,
        reference: ExecutionObjectReference,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> AsyncIterator[VerifiedExecutionObject]:
        raise AssertionError("dispatch-time validation must not read objects")


class _Target:
    """Recognizer/synthesizer double: the lanes only compare this binding revision."""

    def __init__(self, binding_revision_id: str) -> None:
        self.binding_revision_id = binding_revision_id

    def validate_execution_target(self, target: object) -> None:
        return None


class _NoObservation:
    def observe(self, intent: object) -> EngineObservation | None:
        raise AssertionError("a terminal refusal is never reconciled against an engine")


def _inputs() -> FileExecutionInputs:
    return FileExecutionInputs(cast(Any, _Objects()))


def _limits() -> ExecutionInputLimits:
    return ExecutionInputLimits(1000000, 1024, 10)


def _request(
    command: dict[str, Any],
    *,
    submit_intent_id: str,
    binding: str,
    policy: str,
    voice: str | None,
    sample_rate_hz: int | None = None,
    max_output_audio_ms: int | None = None,
    output_stream_id: str | None = None,
) -> SubmitRequest:
    value = cast(JsonValue, command)
    return SubmitRequest(
        identity=SubmissionIdentity(
            submit_intent_id=submit_intent_id,
            lease_id="lease-" + submit_intent_id,
            operation_id="operation-1",
            attempt_id="attempt-1",
            stage_id="stage-1",
            workspace_id="ws-demo",
            pool_id="asr-batch",
            host_node_id="node-input",
            gateway_id="gateway-input",
            instance_id="instance-1",
            instance_generation=1,
            binding_revision_id=binding,
            policy_revision_id=policy,
            voice_revision_id=voice,
            fencing_token=1,
            command_digest=command_digest(value),
            deadline_at=_NOW + timedelta(hours=1),
            max_output_audio_ms=max_output_audio_ms,
            sample_rate_hz=sample_rate_hz,
        ),
        command=value,
        output_stream_id=output_stream_id,
    )


def asr_case() -> tuple[SubmitRequest, _RoutedExecution, type[Exception]]:
    """The alignment parameter belongs to the ASR lane and only ``off`` is served."""
    command = command_example(1)
    command["parameters"]["alignment"] = "on"
    lane = CommandFileAsr(
        inputs=_inputs(),
        decoder=cast(Any, object()),  # never reached: validation performs no I/O
        vad_model=ScriptedVadModel(fallback=0.9),
        recognizer=cast(Any, _Target("binding-input")),
        vad_tuning=VadPolicy("policy-input"),
        max_completion_tokens=64,
        max_call_seconds=2.0,
        input_limits=_limits(),
        decode_limits=AudioDecodeLimits(),
    )
    request = _request(
        command,
        submit_intent_id="intent-asr",
        binding="binding-input",
        policy="policy-input",
        voice=None,
    )
    return (
        request,
        _RoutedExecution(AsrFileStage(lane, 1024), None, None),
        InvalidExecutionRequestError,
    )


def tts_case() -> tuple[SubmitRequest, _RoutedExecution, type[Exception]]:
    """The builtin lane refuses a command without text before any engine call."""
    command = command_example(0)
    command["text"] = ""
    lane = CommandFileTts(
        synthesizer=cast(Any, _Target("binding-tts")),
        encoder=LocalTtsEncoder(),
        binding_policy=TtsPolicy("policy-tts", "Vivian"),
        builtin_voice_binding=BuiltinVoiceBinding("voice-demo", "1", "voice-rev-1", "Vivian"),
        max_payload_bytes=1 << 20,
    )
    request = _request(
        command,
        submit_intent_id="intent-tts",
        binding="binding-tts",
        policy="policy-tts",
        voice="voice-rev-1",
        sample_rate_hz=24000,
        max_output_audio_ms=60000,
        output_stream_id="stream-tts",
    )
    return request, _RoutedExecution(None, lane, None), InvalidExecutionRequestError


def custom_tts_case() -> tuple[SubmitRequest, _RoutedExecution, type[Exception]]:
    """Published clone material without its paired transcript is refused."""
    command = command_example(0)
    command["resources"].update(
        {
            "voice": {"id": "voice-demo", "revision": "1"},
            "voice_kind": "custom",
            "voice_material": dict(_REFERENCE),
        }
    )
    lane = CommandFileCustomTts(
        inputs=_inputs(),
        synthesizer=cast(Any, _Target("binding-voice-clone")),
        encoder=LocalTtsEncoder(),
        binding_policy=VoiceClonePolicy("policy-voice-clone"),
        input_limits=_limits(),
        max_payload_bytes=1 << 20,
    )
    request = _request(
        command,
        submit_intent_id="intent-custom-tts",
        binding="binding-voice-clone",
        policy="policy-voice-clone",
        voice="voice-rev-1",
        sample_rate_hz=24000,
        max_output_audio_ms=60000,
        output_stream_id="stream-custom",
    )
    return request, _RoutedExecution(None, None, None, None, lane), FileCommandInvalidError


def voice_clone_case() -> tuple[SubmitRequest, _RoutedExecution, type[Exception]]:
    """A Base clone without preview text is refused before any reference read."""
    command = command_example(3)
    lane = CommandFileVoiceClone(
        inputs=_inputs(),
        synthesizer=cast(Any, _Target("binding-voice-clone")),
        encoder=LocalTtsEncoder(),
        binding_policy=VoiceClonePolicy("policy-voice-clone"),
        input_limits=_limits(),
        max_payload_bytes=1 << 20,
    )
    request = _request(
        command,
        submit_intent_id="intent-voiceclone",
        binding="binding-voice-clone",
        policy="policy-voice-clone",
        voice=None,
        sample_rate_hz=24000,
        max_output_audio_ms=60000,
        output_stream_id="stream-clone",
    )
    return request, _RoutedExecution(None, None, None, lane), FileCommandInvalidError


def voice_design_case() -> tuple[SubmitRequest, _RoutedExecution, type[Exception]]:
    """A VoiceDesign execution must not name an existing voice revision."""
    command = command_example(2)
    lane = CommandFileVoiceDesign(
        synthesizer=cast(Any, _Target("binding-voice-design")),
        encoder=LocalTtsEncoder(),
        binding_policy=VoiceDesignPolicy("policy-voice-design"),
        max_payload_bytes=1 << 20,
    )
    request = _request(
        command,
        submit_intent_id="intent-voicedesign",
        binding="binding-voice-design",
        policy="policy-voice-design",
        voice="voice-rev-1",
        sample_rate_hz=24000,
        max_output_audio_ms=60000,
        output_stream_id="stream-design",
    )
    return request, _RoutedExecution(None, None, lane), InvalidExecutionRequestError


def alignment_case() -> tuple[SubmitRequest, _RoutedExecution, type[Exception]]:
    """No configured file lane serves the alignment kind: a deterministic refusal."""
    request = _request(
        command_example(5),
        submit_intent_id="intent-alignment",
        binding="binding-input",
        policy="policy-input",
        voice=None,
    )
    return request, _RoutedExecution(None, None, None), InvalidExecutionRequestError


def voice_validation_case() -> tuple[SubmitRequest, _RoutedExecution, type[Exception]]:
    """No configured file lane serves the voice-validation kind either."""
    request = _request(
        command_example(4),
        submit_intent_id="intent-voicevalidation",
        binding="binding-input",
        policy="policy-input",
        voice=None,
    )
    return request, _RoutedExecution(None, None, None), InvalidExecutionRequestError


CASES = [
    pytest.param(asr_case, id="asr-alignment-parameter"),
    pytest.param(tts_case, id="tts-builtin-missing-text"),
    pytest.param(custom_tts_case, id="tts-custom-missing-transcript"),
    pytest.param(voice_clone_case, id="voiceclone-missing-preview"),
    pytest.param(voice_design_case, id="voicedesign-named-voice"),
    pytest.param(alignment_case, id="alignment-unserved-kind"),
    pytest.param(voice_validation_case, id="voicevalidation-unserved-kind"),
]


@contextmanager
def configured(
    routed: Any,
) -> Iterator[
    tuple[InMemorySubmissionLedger, FileExecutionWorker[_Stores], SubmitExecutionService]
]:
    ledger = InMemorySubmissionLedger()
    ingress = fake_ingress(node_id="node-input", gateway_id="gateway-input", timeout_seconds=0.05)
    worker = FileExecutionWorker(
        ingress=ingress,
        ledger=ledger,
        execution=routed,
        results=_Stores(None, None, None),
        limits=FileExecutionLimits(1024, 16384, 2, 10),
        engine_request_prefix="file-command",
    )
    worker.open()
    submit = SubmitExecutionService(
        ingress=ingress,
        ledger=ledger,
        dispatch=OriginalCommandDispatch(commands=ledger, engine=worker, max_document_bytes=16384),
        observation=worker,
        committed_execution=worker,
    )
    try:
        yield ledger, worker, submit
    finally:
        worker.close()


@pytest.mark.parametrize("case", CASES)
async def test_a_structural_dispatch_refusal_is_a_deterministic_terminal(
    case: Callable[[], tuple[SubmitRequest, _RoutedExecution, type[Exception]]],
) -> None:
    request, routed, expected_error = case()
    with pytest.raises(expected_error):
        routed.validate_request(request)

    with configured(routed) as (ledger, worker, submit):
        submit.submit(request)
        stored = ledger.get(request.identity.submit_intent_id)
        assert stored is not None
        # The revert guard: mapping a deterministic refusal back to
        # ENGINE_TRANSPORT_UNKNOWN turns this assertion red.
        assert stored.error_code is ExecutionErrorCode.INVALID_EXECUTION_REQUEST
        assert stored.execution_state is ExecutionState.FAILED
        assert stored.cleanup_state is CleanupState.NOT_REQUIRED
        assert stored.dispatched_at is None
        assert stored.engine_request_id is None
        assert stored.stop_evidence_kind is StopEvidenceKind.NONE
        assert not stored.execution_state.holds_capacity
        assert worker.running


async def test_a_local_dispatch_failure_that_is_not_a_refusal_stays_unknown() -> None:
    """An unexpected local failure keeps the conservative transport fact."""

    class Broken:
        def validate_request(self, request: SubmitRequest) -> SubmitRequest:
            raise ConnectionError("synthetic unexpected local failure")

        async def stage_result(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a failed dispatch never stages")

    request, _, _ = asr_case()
    with configured(Broken()) as (ledger, worker, submit):
        submit.submit(request)
        stored = ledger.get(request.identity.submit_intent_id)
        assert stored is not None
        # The fallback guard: reclassifying every dispatch exception as a refusal
        # turns this assertion red.
        assert stored.error_code is ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN
        assert stored.execution_state is ExecutionState.FAILED
        assert stored.cleanup_state is CleanupState.NOT_REQUIRED
        assert stored.dispatched_at is None
        assert worker.running


def test_a_lost_engine_response_stays_transport_unknown_and_holds_capacity() -> None:
    class LostEngine:
        def dispatch(self, request: SubmitRequest) -> DispatchOutcome:
            raise ConnectionError("synthetic lost response")

    request, _, _ = asr_case()
    ledger = InMemorySubmissionLedger()
    ingress = fake_ingress(node_id="node-input", gateway_id="gateway-input")
    submit = SubmitExecutionService(
        ingress=ingress,
        ledger=ledger,
        dispatch=OriginalCommandDispatch(
            commands=ledger, engine=cast(Any, LostEngine()), max_document_bytes=16384
        ),
        observation=_NoObservation(),
    )

    submit.submit(request)
    stored = ledger.get(request.identity.submit_intent_id)
    assert stored is not None
    assert stored.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    assert stored.error_code is ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN
    assert stored.cleanup_state is CleanupState.QUARANTINED
    assert stored.dispatched_at is not None
    assert stored.execution_state.holds_capacity


async def test_a_deterministic_refusal_is_never_retried_or_recovered() -> None:
    request, routed, _ = custom_tts_case()
    with configured(routed) as (ledger, worker, submit):
        submit.submit(request)
        stored = ledger.get(request.identity.submit_intent_id)
        assert stored is not None

        # Startup recovery only reads submitting/executing/stopping work, so a
        # deterministic refusal is never picked up as unfinished again.
        assert (
            ledger.list_unfinished(gateway_id="gateway-input", host_node_id="node-input", limit=10)
            == []
        )

        # The unknown marking a recovery path could attempt is refused outright:
        # a terminal refusal cannot be turned into an ambiguous execution.
        with pytest.raises(ExecutionCleanupPendingError):
            ledger.mark_execution_unknown(
                MarkExecutionUnknown.for_intent(
                    stored,
                    reason_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
                    at=datetime.now(UTC),
                )
            )

        # A repeated submit is an idempotent read of the original identity; it can
        # never create a second execution or dispatch the command again.
        replay = submit.submit(request)
        assert replay.idempotent_replay
        assert replay.intent.execution_state is ExecutionState.FAILED
        assert replay.intent.error_code is ExecutionErrorCode.INVALID_EXECUTION_REQUEST
        assert ledger.get(request.identity.submit_intent_id) == replay.intent
        assert len(ledger._intents) == 1
        assert worker._preparations == {}

        # Querying never reconciles it against an engine either.
        assert submit.query(request.identity.submit_intent_id) == stored
