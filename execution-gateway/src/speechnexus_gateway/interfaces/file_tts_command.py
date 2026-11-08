"""Consume an original file TTS command through the native bounded synthesis pipeline.

This boundary freezes one supported command, performs at most one engine call and
can stage the encoded result privately. It does not admit work, record a terminal
outcome or publish artifacts; it reports the engine's terminal answer to the
execution owner, and only that owner records a stop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from speechnexus_contracts.execution import parse

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.application.file_tts import (
    FileCustomTtsSynthesis,
    FileTtsResult,
    FileTtsSynthesis,
)
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputLimits,
    ExecutionObjectReference,
)
from speechnexus_gateway.domain.file_execution import (
    FileCommandInvalidError,
    FileStagingOutcome,
)
from speechnexus_gateway.domain.languages import AUTO_LANGUAGE
from speechnexus_gateway.domain.submission import (
    MAX_ORIGINAL_COMMAND_BYTES,
    InvalidExecutionRequestError,
    SubmitRequest,
)
from speechnexus_gateway.domain.terminal import EngineTerminalResponse
from speechnexus_gateway.domain.tts import (
    BuiltinVoiceBinding,
    TtsCallError,
    TtsDeliveryEncoder,
    TtsDeliveryFormat,
    TtsExecutionTarget,
    TtsFailure,
    TtsPolicy,
    TtsRequest,
    TtsSynthesizer,
    pcm16_mono_bytes,
)
from speechnexus_gateway.domain.tts_results import TtsResultStore
from speechnexus_gateway.domain.voice_clone import (
    MAX_REFERENCE_TEXT_CHARS,
    ReferenceAudio,
    VoiceCloneCallError,
    VoiceCloneExecutionTarget,
    VoiceCloneFailure,
    VoiceClonePolicy,
    VoiceCloneRequest,
    VoiceCloneSynthesizer,
    reference_text_fits_duration,
)

_DELIVERY_FORMATS = {target.value: target for target in TtsDeliveryFormat}
_CUSTOM_STORAGE_FIELDS = {
    "asset_or_artifact_id",
    "workspace_id",
    "storage_bucket",
    "object_key",
    "content_sha256",
    "byte_length",
}
_MAX_CUSTOM_TTS_CHARS = 2000


def _invalid(message: str) -> InvalidExecutionRequestError:
    return InvalidExecutionRequestError(message)


def _custom_request_bounds(policy: VoiceClonePolicy, max_output_audio_ms: int) -> tuple[int, int]:
    """Tighten the approved Base binding to the duration this request declared."""
    bounded = min(max_output_audio_ms, policy.max_audio_ms)
    return bounded, min(policy.max_pcm_bytes, pcm16_mono_bytes(bounded, policy.sample_rate_hz))


def _request_bounds(policy: TtsPolicy, max_output_audio_ms: int) -> tuple[int, int]:
    """Tighten the approved binding to the duration this request declared.

    A request ceiling below the binding must bound both the audio clock and the
    source PCM, otherwise the engine may return audio the request never allowed.
    The declared ceiling is already at least one frame by the submission
    identity, so the derived bound always satisfies the policy floor.
    """
    bounded = min(max_output_audio_ms, policy.max_audio_ms)
    return bounded, min(policy.max_pcm_bytes, pcm16_mono_bytes(bounded, policy.sample_rate_hz))


class CommandFileTts:
    """A single synthesis per command, with no independent policy defaults.

    The approved binding supplies the speaker, engine format, clock and limits.
    Command values only override the fields they own; anything this unit cannot
    honour is rejected before any engine call or object write.
    """

    def __init__(
        self,
        *,
        synthesizer: TtsSynthesizer,
        encoder: TtsDeliveryEncoder,
        binding_policy: TtsPolicy,
        builtin_voice_binding: BuiltinVoiceBinding,
        max_payload_bytes: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(binding_policy, TtsPolicy):
            raise ValueError("an approved TTS binding policy is required")
        if (
            not isinstance(builtin_voice_binding, BuiltinVoiceBinding)
            or builtin_voice_binding.native_speaker_id != binding_policy.voice
        ):
            raise ValueError("the builtin voice binding must match the native TTS speaker")
        if type(max_payload_bytes) is not int or not 1024 <= max_payload_bytes <= 64 * 1024 * 1024:
            raise ValueError("TTS delivery limit is out of range")
        self._synthesizer = synthesizer
        self._encoder = encoder
        self._binding_policy = binding_policy
        self._builtin_voice_binding = builtin_voice_binding
        self._max_payload_bytes = max_payload_bytes
        self._now = now

    def _resolve(
        self, request: SubmitRequest
    ) -> tuple[SubmitRequest, TtsRequest, TtsPolicy, TtsDeliveryFormat]:
        try:
            snapshot = replace(request, command=deepcopy(request.command))
            if (
                len(snapshot.request_document.encode("utf-8")) > MAX_ORIGINAL_COMMAND_BYTES
                or command_digest(snapshot.command) != snapshot.identity.command_digest
            ):
                raise ValueError
            wire = snapshot.effective_request
            if not isinstance(wire, dict):
                raise ValueError
            command = parse("SubmitExecutionRequest", wire["execution_request"])["command"]
            if (
                command["kind"] != "tts"
                or command["task_kind"] != "tts_file"
                or command["stage_role"] != "tts_connector"
            ):
                raise ValueError
            identity = snapshot.identity
            if (
                snapshot.output_stream_id is None
                or identity.sample_rate_hz != self._binding_policy.sample_rate_hz
                or identity.max_output_audio_ms is None
                or identity.max_output_audio_ms > self._binding_policy.max_audio_ms
                or identity.binding_revision_id != self._synthesizer.binding_revision_id
                or identity.policy_revision_id != self._binding_policy.config_revision
            ):
                raise ValueError
            self._validate_target(snapshot)
            resources = command["resources"]
            voice = resources["voice"]
            if (
                not isinstance(voice, dict)
                or resources["voice_kind"] != "builtin"
                or resources["voice_material"] is not None
                or resources["vocabulary"]
                or resources["pronunciation"]
                or resources["vocabulary_source"] is not None
                or resources["pronunciation_source"] is not None
                or identity.voice_revision_id != self._builtin_voice_binding.voice_revision_id
                or voice["id"] != self._builtin_voice_binding.voice_id
                or voice["revision"] != self._builtin_voice_binding.voice_revision
            ):
                raise ValueError
            text = command["text"]
            if not isinstance(text, str) or command["text_stream"] is not None:
                raise ValueError
            parameters = command["parameters"]
            planner = parameters["planner"]
            if (
                parameters.get("reference_text") is not None
                or parameters["text_commit_mode"] != "explicit"
                or parameters["audio_delivery"] != "complete"
                or parameters["pronunciation_revision"] is not None
                or not isinstance(planner, dict)
                or not 1 <= len(text) <= planner["max_chars"]
            ):
                raise ValueError
            delivery = _DELIVERY_FORMATS[parameters["response_format"]]
            max_audio_ms, max_pcm_bytes = _request_bounds(
                self._binding_policy, identity.max_output_audio_ms
            )
            policy = replace(
                self._binding_policy,
                instructions=parameters["instructions"] or "",
                speed=parameters["speed"],
                language=parameters["language"],
                seed=parameters["seed"],
                max_audio_ms=max_audio_ms,
                max_pcm_bytes=max_pcm_bytes,
            )
            frozen = TtsRequest(identity.submit_intent_id, text)
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            OverflowError,
            RecursionError,
        ):
            raise _invalid(
                "file TTS command, capability or configured binding does not match"
            ) from None
        return snapshot, frozen, policy, delivery

    def _validate_target(self, request: SubmitRequest) -> None:
        identity = request.identity
        self._synthesizer.validate_execution_target(
            TtsExecutionTarget(
                host_node_id=identity.host_node_id,
                gateway_id=identity.gateway_id,
                pool_id=identity.pool_id,
                instance_id=identity.instance_id,
                instance_generation=identity.instance_generation,
                binding_revision_id=identity.binding_revision_id,
            )
        )

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        """Freeze the supported command before accepting work; no external I/O."""
        snapshot, _, _, _ = self._resolve(request)
        return snapshot

    def _remaining(self, snapshot: SubmitRequest) -> float:
        return (snapshot.identity.deadline_at - self._now()).total_seconds()

    def _execution(
        self,
        snapshot: SubmitRequest,
        frozen: TtsRequest,
        policy: TtsPolicy,
        delivery: TtsDeliveryFormat,
        cancelled: Callable[[], bool],
    ) -> FileTtsSynthesis:
        holder: list[FileTtsSynthesis] = []

        def check() -> bool:
            dispatched = bool(holder) and holder[0].has_dispatched
            if self._now() >= snapshot.identity.deadline_at:
                raise TtsCallError(TtsFailure.DEADLINE_EXCEEDED, request_may_have_run=dispatched)
            try:
                self._validate_target(snapshot)
            except ValueError:
                raise TtsCallError(
                    TtsFailure.RESPONSE_INVALID, request_may_have_run=dispatched
                ) from None
            return cancelled()

        synthesis = FileTtsSynthesis(
            synthesizer=self._synthesizer,
            encoder=self._encoder,
            request=frozen,
            policy=policy,
            delivery_format=delivery,
            max_payload_bytes=self._max_payload_bytes,
            cancelled=check,
            execution_id=snapshot.identity.submit_intent_id,
        )
        holder.append(synthesis)
        return synthesis

    async def synthesize(
        self, request: SubmitRequest, *, cancelled: Callable[[], bool]
    ) -> FileTtsResult:
        """One bounded synthesis; the caller still owns admission and metering."""
        snapshot, frozen, policy, delivery = self._resolve(request)
        remaining = self._remaining(snapshot)
        if remaining <= 0:
            raise TtsCallError(TtsFailure.DEADLINE_EXCEEDED, request_may_have_run=False)
        synthesis = self._execution(snapshot, frozen, policy, delivery, cancelled)
        try:
            async with asyncio.timeout(remaining):
                return await synthesis.run()
        except TimeoutError:
            raise TtsCallError(
                TtsFailure.DEADLINE_EXCEEDED, request_may_have_run=synthesis.has_dispatched
            ) from None

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: TtsResultStore,
        cancelled: Callable[[], bool],
    ) -> FileStagingOutcome:
        """One synthesis and verified private PUT; never a meter or budget write.

        The outcome carries the engine terminal fact for this execution's single
        request; only the execution owner may turn that fact into a stop.
        """
        snapshot, frozen, policy, delivery = self._resolve(request)
        remaining = self._remaining(snapshot)
        if remaining <= 0:
            raise TtsCallError(TtsFailure.DEADLINE_EXCEEDED, request_may_have_run=False)
        synthesis = self._execution(snapshot, frozen, policy, delivery, cancelled)
        try:
            async with asyncio.timeout(remaining):
                receipt = await synthesis.stage_result(
                    workspace_id=snapshot.identity.workspace_id,
                    request_digest=snapshot.request_digest,
                    store=store,
                )
        except TimeoutError:
            raise TtsCallError(
                TtsFailure.DEADLINE_EXCEEDED, request_may_have_run=synthesis.has_dispatched
            ) from None
        return FileStagingOutcome(
            receipt,
            EngineTerminalResponse(
                snapshot.identity.submit_intent_id, (frozen.request_id,), self._now()
            ),
        )


@dataclass(frozen=True, slots=True)
class _CustomTtsMetadata:
    text: str = field(repr=False)
    language: str
    reference_text: str = field(repr=False)
    reference: ExecutionObjectReference = field(repr=False)
    delivery: TtsDeliveryFormat


def _custom_invalid(message: str) -> FileCommandInvalidError:
    return FileCommandInvalidError(message)


class CommandFileCustomTts:
    """One Base synthesis per file-TTS command that names published clone material.

    The approved Base binding supplies the engine target, clock and limits. The
    command supplies the text, the immutable custom voice material reference and
    the paired transcript. A builtin command, a missing material reference or a
    missing transcript is refused before any object read or engine call; the
    gateway never falls back to a configured builtin speaker.
    """

    def __init__(
        self,
        *,
        inputs: FileExecutionInputs,
        synthesizer: VoiceCloneSynthesizer,
        encoder: TtsDeliveryEncoder,
        binding_policy: VoiceClonePolicy,
        input_limits: ExecutionInputLimits,
        max_payload_bytes: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(binding_policy, VoiceClonePolicy):
            raise ValueError("an approved Base custom TTS binding policy is required")
        if not isinstance(input_limits, ExecutionInputLimits):
            raise ValueError("custom TTS inputs require explicit consumption limits")
        if not 1024 <= max_payload_bytes <= 64 * 1024 * 1024:
            raise ValueError("custom TTS delivery limit is out of range")
        self._inputs = inputs
        self._synthesizer = synthesizer
        self._encoder = encoder
        self._binding_policy = binding_policy
        self._input_limits = input_limits
        self._max_payload_bytes = max_payload_bytes
        self._now = now

    def _resolve(
        self, request: SubmitRequest
    ) -> tuple[SubmitRequest, _CustomTtsMetadata, VoiceClonePolicy]:
        try:
            snapshot = replace(request, command=deepcopy(request.command))
            if (
                len(snapshot.request_document.encode("utf-8")) > MAX_ORIGINAL_COMMAND_BYTES
                or command_digest(snapshot.command) != snapshot.identity.command_digest
            ):
                raise ValueError
            wire = snapshot.effective_request
            if not isinstance(wire, dict):
                raise ValueError
            command = parse("SubmitExecutionRequest", wire["execution_request"])["command"]
            if (
                command["kind"] != "tts"
                or command["task_kind"] != "tts_file"
                or command["stage_role"] != "tts_connector"
            ):
                raise ValueError
            identity = snapshot.identity
            if (
                snapshot.output_stream_id is None
                or identity.sample_rate_hz != self._binding_policy.sample_rate_hz
                or identity.max_output_audio_ms is None
                or identity.max_output_audio_ms > self._binding_policy.max_audio_ms
                or identity.binding_revision_id != self._synthesizer.binding_revision_id
                or identity.policy_revision_id != self._binding_policy.config_revision
                or type(identity.voice_revision_id) is not str
                or not identity.voice_revision_id
            ):
                raise ValueError
            self._validate_target(snapshot)
            resources = command["resources"]
            voice = resources["voice"]
            if (
                not isinstance(voice, dict)
                or set(voice) != {"id", "revision"}
                or not isinstance(voice["id"], str)
                or not isinstance(voice["revision"], str)
                or resources["voice_kind"] != "custom"
                or not isinstance(resources["voice_material"], dict)
                or set(resources["voice_material"]) != _CUSTOM_STORAGE_FIELDS
                or resources["vocabulary"]
                or resources["pronunciation"]
                or resources["vocabulary_source"] is not None
                or resources["pronunciation_source"] is not None
            ):
                raise ValueError
            text = command["text"]
            if (
                not isinstance(text, str)
                or command["text_stream"] is not None
                or not 1 <= len(text) <= _MAX_CUSTOM_TTS_CHARS
            ):
                raise ValueError
            parameters = command["parameters"]
            planner = parameters["planner"]
            reference_text = parameters["reference_text"]
            if (
                parameters["text_commit_mode"] != "explicit"
                or parameters["audio_delivery"] != "complete"
                or parameters["pronunciation_revision"] is not None
                or parameters["instructions"]
                or parameters["speed"] != 1
                or not isinstance(reference_text, str)
                or not reference_text.strip()
                or not 1 <= len(reference_text) <= MAX_REFERENCE_TEXT_CHARS
                or "\x00" in reference_text
                or not isinstance(planner, dict)
                or not 1 <= len(text) <= planner["max_chars"]
            ):
                raise ValueError
            delivery = _DELIVERY_FORMATS[parameters["response_format"]]
            reference = ExecutionObjectReference(**resources["voice_material"])
            if (
                reference.workspace_id != identity.workspace_id
                or reference.byte_length > self._binding_policy.max_reference_audio_bytes
                or reference.byte_length > self._input_limits.max_bytes
            ):
                raise ValueError
            max_audio_ms, max_pcm_bytes = _custom_request_bounds(
                self._binding_policy, identity.max_output_audio_ms
            )
            policy = replace(
                self._binding_policy,
                max_audio_ms=max_audio_ms,
                max_pcm_bytes=max_pcm_bytes,
            )
            metadata = _CustomTtsMetadata(
                text,
                parameters["language"],
                reference_text,
                reference,
                delivery,
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            OverflowError,
            RecursionError,
        ):
            raise _custom_invalid(
                "custom TTS command, published material or configured Base binding does not match"
            ) from None
        if not isinstance(metadata.language, str) or (
            metadata.language != AUTO_LANGUAGE
            and metadata.language.split("-", 1)[0].lower()
            not in self._binding_policy.supported_languages
        ):
            raise _custom_invalid(
                "the requested language has no verified value in the configured Base binding"
            )
        return snapshot, metadata, policy

    def _validate_target(self, request: SubmitRequest) -> None:
        identity = request.identity
        self._synthesizer.validate_execution_target(
            VoiceCloneExecutionTarget(
                host_node_id=identity.host_node_id,
                gateway_id=identity.gateway_id,
                pool_id=identity.pool_id,
                instance_id=identity.instance_id,
                instance_generation=identity.instance_generation,
                binding_revision_id=identity.binding_revision_id,
            )
        )

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        """Freeze the supported command before accepting work; no external I/O."""
        snapshot, _, _ = self._resolve(request)
        return snapshot

    def _remaining(self, snapshot: SubmitRequest) -> float:
        return (snapshot.identity.deadline_at - self._now()).total_seconds()

    def _failure(self, snapshot: SubmitRequest, *, dispatched: bool) -> VoiceCloneCallError:
        if self._now() >= snapshot.identity.deadline_at:
            return VoiceCloneCallError(
                VoiceCloneFailure.DEADLINE_EXCEEDED, request_may_have_run=dispatched
            )
        try:
            self._validate_target(snapshot)
        except ValueError:
            return VoiceCloneCallError(
                VoiceCloneFailure.RESPONSE_INVALID, request_may_have_run=dispatched
            )
        return VoiceCloneCallError(VoiceCloneFailure.CANCELLED, request_may_have_run=dispatched)

    async def _frozen(
        self,
        snapshot: SubmitRequest,
        metadata: _CustomTtsMetadata,
        policy: VoiceClonePolicy,
        cancelled: Callable[[], bool],
    ) -> VoiceCloneRequest:
        prepared = await self._inputs.read_voice_material(
            snapshot, limits=self._input_limits, cancelled=cancelled
        )
        try:
            audio = ReferenceAudio.from_wav(
                prepared.payload,
                sample_rates=policy.reference_sample_rates,
                min_duration_ms=policy.min_reference_audio_ms,
                max_duration_ms=policy.max_reference_audio_ms,
                max_bytes=policy.max_reference_audio_bytes,
            )
        except ValueError:
            raise _custom_invalid(
                "custom TTS reference audio is not an approved WAV shape"
            ) from None
        if not reference_text_fits_duration(metadata.reference_text, audio):
            # The gateway cannot prove the transcript matches the clip, but it can
            # refuse a pairing no speaker could produce at ordinary tempo.
            raise _custom_invalid(
                "custom TTS reference transcript does not match the clip duration"
            )
        try:
            return VoiceCloneRequest(
                snapshot.identity.submit_intent_id,
                metadata.text,
                metadata.language,
                audio,
                metadata.reference_text,
            )
        except ValueError:
            raise _custom_invalid("custom TTS reference material is not approved") from None

    async def synthesize(
        self, request: SubmitRequest, *, cancelled: Callable[[], bool]
    ) -> FileTtsResult:
        """One bounded synthesis; the caller still owns admission and metering."""
        snapshot, metadata, policy = self._resolve(request)
        remaining = self._remaining(snapshot)
        if remaining <= 0:
            raise VoiceCloneCallError(
                VoiceCloneFailure.DEADLINE_EXCEEDED, request_may_have_run=False
            )
        holder: list[FileCustomTtsSynthesis] = []

        def check() -> bool:
            dispatched = bool(holder) and holder[0].has_dispatched
            if cancelled():
                raise self._failure(snapshot, dispatched=dispatched)
            if self._now() >= snapshot.identity.deadline_at:
                raise self._failure(snapshot, dispatched=dispatched)
            try:
                self._validate_target(snapshot)
            except ValueError:
                raise self._failure(snapshot, dispatched=dispatched) from None
            return False

        try:
            async with asyncio.timeout(remaining):
                frozen = await self._frozen(snapshot, metadata, policy, check)
                synthesis = FileCustomTtsSynthesis(
                    synthesizer=self._synthesizer,
                    encoder=self._encoder,
                    request=frozen,
                    policy=policy,
                    delivery_format=metadata.delivery,
                    max_payload_bytes=self._max_payload_bytes,
                    cancelled=check,
                    execution_id=snapshot.identity.submit_intent_id,
                )
                holder.append(synthesis)
                return await synthesis.run()
        except TimeoutError:
            raise VoiceCloneCallError(
                VoiceCloneFailure.DEADLINE_EXCEEDED,
                request_may_have_run=bool(holder) and holder[0].has_dispatched,
            ) from None

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: TtsResultStore,
        cancelled: Callable[[], bool],
    ) -> FileStagingOutcome:
        """One synthesis and verified private PUT; never a meter or budget write."""
        snapshot, metadata, policy = self._resolve(request)
        remaining = self._remaining(snapshot)
        if remaining <= 0:
            raise VoiceCloneCallError(
                VoiceCloneFailure.DEADLINE_EXCEEDED, request_may_have_run=False
            )
        holder: list[FileCustomTtsSynthesis] = []

        def check() -> bool:
            dispatched = bool(holder) and holder[0].has_dispatched
            if cancelled():
                raise self._failure(snapshot, dispatched=dispatched)
            if self._now() >= snapshot.identity.deadline_at:
                raise self._failure(snapshot, dispatched=dispatched)
            try:
                self._validate_target(snapshot)
            except ValueError:
                raise self._failure(snapshot, dispatched=dispatched) from None
            return False

        try:
            async with asyncio.timeout(remaining):
                frozen = await self._frozen(snapshot, metadata, policy, check)
                synthesis = FileCustomTtsSynthesis(
                    synthesizer=self._synthesizer,
                    encoder=self._encoder,
                    request=frozen,
                    policy=policy,
                    delivery_format=metadata.delivery,
                    max_payload_bytes=self._max_payload_bytes,
                    cancelled=check,
                    execution_id=snapshot.identity.submit_intent_id,
                )
                holder.append(synthesis)
                receipt = await synthesis.stage_result(
                    workspace_id=snapshot.identity.workspace_id,
                    request_digest=snapshot.request_digest,
                    store=store,
                )
        except TimeoutError:
            raise VoiceCloneCallError(
                VoiceCloneFailure.DEADLINE_EXCEEDED,
                request_may_have_run=bool(holder) and holder[0].has_dispatched,
            ) from None
        return FileStagingOutcome(
            receipt,
            EngineTerminalResponse(
                snapshot.identity.submit_intent_id, (frozen.request_id,), self._now()
            ),
        )
