"""Consume an original Base voice-clone command through one native bounded clone.

This boundary freezes one supported command, verifies the private reference clip
inside the gateway before any engine call, performs at most one engine call and
can stage the preview audio privately. It does not admit work, record a terminal
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
from speechnexus_gateway.application.voice_clone import (
    VoiceCloneSynthesis,
)
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputLimits,
    ExecutionObjectReference,
)
from speechnexus_gateway.domain.file_execution import FileStagingOutcome
from speechnexus_gateway.domain.submission import (
    MAX_ORIGINAL_COMMAND_BYTES,
    InvalidExecutionRequestError,
    SubmitRequest,
)
from speechnexus_gateway.domain.terminal import EngineTerminalResponse
from speechnexus_gateway.domain.tts import (
    TtsDeliveryEncoder,
    TtsDeliveryFormat,
    pcm16_mono_bytes,
)
from speechnexus_gateway.domain.tts_results import TtsResultStore
from speechnexus_gateway.domain.voice_clone import (
    ReferenceAudio,
    VoiceCloneCallError,
    VoiceCloneExecutionTarget,
    VoiceCloneFailure,
    VoiceClonePolicy,
    VoiceCloneRequest,
    VoiceCloneSynthesizer,
    reference_text_fits_duration,
)

_EMPTY_RESOURCES: dict[str, object] = {
    "voice": None,
    "voice_material": None,
    "vocabulary": [],
    "pronunciation": [],
    "voice_kind": None,
    "vocabulary_source": None,
    "pronunciation_source": None,
}
_STORAGE_FIELDS = {
    "asset_or_artifact_id",
    "workspace_id",
    "storage_bucket",
    "object_key",
    "content_sha256",
    "byte_length",
}
_REFERENCE_AUDIO_FIELDS = {"storage", "media_type", "sample_rate_hz", "channels"}


def _invalid(message: str) -> InvalidExecutionRequestError:
    return InvalidExecutionRequestError(message)


@dataclass(frozen=True, slots=True)
class _CloneMetadata:
    preview_text: str = field(repr=False)
    language: str
    reference_text: str | None = field(repr=False)
    reference: ExecutionObjectReference = field(repr=False)
    sample_rate_hz: int


class CommandFileVoiceClone:
    """A single clone per command, with no independent policy defaults.

    The approved Base binding supplies the task, engine format, clock and limits.
    Command values only supply the preview text, language, paired transcript and
    the storage reference of the private reference clip; anything this unit
    cannot honour is rejected before any engine call.
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
            raise ValueError("an approved voice clone binding policy is required")
        if not isinstance(input_limits, ExecutionInputLimits):
            raise ValueError("voice clone inputs require explicit consumption limits")
        if not 1024 <= max_payload_bytes <= 64 * 1024 * 1024:
            raise ValueError("voice clone delivery limit is out of range")
        self._inputs = inputs
        self._synthesizer = synthesizer
        self._encoder = encoder
        self._binding_policy = binding_policy
        self._input_limits = input_limits
        self._max_payload_bytes = max_payload_bytes
        self._now = now

    def _resolve(
        self, request: SubmitRequest
    ) -> tuple[SubmitRequest, _CloneMetadata, VoiceClonePolicy]:
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
                command["kind"] != "voiceclone"
                or command["task_kind"] != "voice_clone"
                or command["stage_role"] != "voice_clone_base"
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
                or identity.voice_revision_id is not None
                or command["resources"] != _EMPTY_RESOURCES
                or command["source_use_confirmed"] is not True
            ):
                raise ValueError
            self._validate_target(snapshot)
            preview_text = command["preview_text"]
            language = command["language"]
            reference_text = command["reference_text"]
            if (
                not isinstance(preview_text, str)
                or not 1 <= len(preview_text) <= 2000
                or "\x00" in preview_text
                or not isinstance(language, str)
            ):
                raise ValueError
            if reference_text is not None:
                if (
                    not isinstance(reference_text, str)
                    or not 1 <= len(reference_text) <= 10000
                    or "\x00" in reference_text
                ):
                    raise ValueError
            elif not self._binding_policy.allow_x_vector_only:
                # ICL keeps the transcript paired with its clip; dropping it is
                # an explicit binding decision because similarity may drop.
                raise ValueError
            audio = command["reference_audio"]
            if (
                not isinstance(audio, dict)
                or set(audio) != _REFERENCE_AUDIO_FIELDS
                or audio["media_type"] != "audio/wav"
                or audio["channels"] != 1
                or type(audio["sample_rate_hz"]) is not int
                or audio["sample_rate_hz"] not in self._binding_policy.reference_sample_rates
                or not isinstance(audio["storage"], dict)
                or set(audio["storage"]) != _STORAGE_FIELDS
            ):
                raise ValueError
            reference = ExecutionObjectReference(**audio["storage"])
            if (
                reference.workspace_id != identity.workspace_id
                or reference.byte_length > self._binding_policy.max_reference_audio_bytes
                or reference.byte_length > self._input_limits.max_bytes
            ):
                raise ValueError
            max_audio_ms, max_pcm_bytes = _request_bounds(
                self._binding_policy, identity.max_output_audio_ms
            )
            policy = replace(
                self._binding_policy,
                max_audio_ms=max_audio_ms,
                max_pcm_bytes=max_pcm_bytes,
            )
            metadata = _CloneMetadata(
                preview_text,
                language,
                reference_text,
                reference,
                audio["sample_rate_hz"],
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            OverflowError,
            RecursionError,
        ):
            raise _invalid(
                "voice clone command, reference material or configured binding does not match"
            ) from None
        if metadata.language == "auto" or (
            metadata.language.split("-", 1)[0].lower()
            not in self._binding_policy.supported_languages
        ):
            raise _invalid(
                "the requested language has no verified value in the configured voice clone binding"
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
        metadata: _CloneMetadata,
        policy: VoiceClonePolicy,
        cancelled: Callable[[], bool],
    ) -> VoiceCloneRequest:
        prepared = await self._inputs.read_voice_clone(
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
            raise _invalid("voice clone reference audio is not an approved WAV shape") from None
        if audio.sample_rate_hz != metadata.sample_rate_hz:
            raise _invalid("voice clone reference audio contradicts its declared sample rate")
        if metadata.reference_text is not None and not reference_text_fits_duration(
            metadata.reference_text, audio
        ):
            raise _invalid("voice clone reference transcript does not match the clip duration")
        try:
            return VoiceCloneRequest(
                snapshot.identity.submit_intent_id,
                metadata.preview_text,
                metadata.language,
                audio,
                metadata.reference_text,
            )
        except ValueError:
            raise _invalid("voice clone reference material is not approved") from None

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
        holder: list[VoiceCloneSynthesis] = []

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
                synthesis = VoiceCloneSynthesis(
                    synthesizer=self._synthesizer,
                    encoder=self._encoder,
                    request=frozen,
                    policy=policy,
                    delivery_format=TtsDeliveryFormat(self._binding_policy.engine_format.value),
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
                snapshot.identity.submit_intent_id,
                (snapshot.identity.submit_intent_id,),
                self._now(),
            ),
        )


def _request_bounds(policy: VoiceClonePolicy, max_output_audio_ms: int) -> tuple[int, int]:
    """Tighten the approved binding to the duration this request declared."""
    bounded = min(max_output_audio_ms, policy.max_audio_ms)
    return bounded, min(policy.max_pcm_bytes, pcm16_mono_bytes(bounded, policy.sample_rate_hz))
