"""Consume an original VoiceDesign command through the native bounded design stage.

This boundary freezes one supported command, performs at most one engine call and
can stage the preview audio privately. It does not admit work, record a terminal
outcome or publish artifacts; it reports the engine's terminal answer to the
execution owner, and only that owner records a stop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from speechnexus_contracts.execution import parse

from speechnexus_gateway.application.voice_design import (
    FileVoiceDesignResult,
    VoiceDesignSynthesis,
)
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.file_execution import FileStagingOutcome
from speechnexus_gateway.domain.languages import AUTO_LANGUAGE
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
from speechnexus_gateway.domain.voice_design import (
    VoiceDesignCallError,
    VoiceDesignExecutionTarget,
    VoiceDesignFailure,
    VoiceDesignPolicy,
    VoiceDesignRequest,
    VoiceDesignSynthesizer,
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


def _invalid(message: str) -> InvalidExecutionRequestError:
    return InvalidExecutionRequestError(message)


class CommandFileVoiceDesign:
    """A single design per command, with no independent policy defaults.

    The approved VoiceDesign binding supplies the task, engine format, clock and
    limits. Command values only supply the description, preview text, language and
    seed; anything this unit cannot honour is rejected before any engine call.
    """

    def __init__(
        self,
        *,
        synthesizer: VoiceDesignSynthesizer,
        encoder: TtsDeliveryEncoder,
        binding_policy: VoiceDesignPolicy,
        max_payload_bytes: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(binding_policy, VoiceDesignPolicy):
            raise ValueError("an approved VoiceDesign binding policy is required")
        if type(max_payload_bytes) is not int or not 1024 <= max_payload_bytes <= 64 * 1024 * 1024:
            raise ValueError("VoiceDesign delivery limit is out of range")
        self._synthesizer = synthesizer
        self._encoder = encoder
        self._binding_policy = binding_policy
        self._max_payload_bytes = max_payload_bytes
        self._now = now

    def _resolve(
        self, request: SubmitRequest
    ) -> tuple[SubmitRequest, VoiceDesignRequest, VoiceDesignPolicy]:
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
                command["kind"] != "voicedesign"
                or command["task_kind"] != "voice_design"
                or command["stage_role"] != "voice_design"
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
            ):
                raise ValueError
            self._validate_target(snapshot)
            description = command["description"]
            preview_text = command["preview_text"]
            language = command["language"]
            seed = command["seed"]
            if (
                not isinstance(description, str)
                or not isinstance(preview_text, str)
                or not isinstance(language, str)
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
            frozen = VoiceDesignRequest(
                identity.submit_intent_id,
                description,
                preview_text,
                language,
                seed,
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
                "VoiceDesign command, capability or configured binding does not match"
            ) from None
        if (
            frozen.language != AUTO_LANGUAGE
            and frozen.language.split("-", 1)[0].lower()
            not in self._binding_policy.supported_languages
        ):
            raise _invalid(
                "the requested language has no verified value in the configured VoiceDesign binding"
            )
        return snapshot, frozen, policy

    def _validate_target(self, request: SubmitRequest) -> None:
        identity = request.identity
        self._synthesizer.validate_execution_target(
            VoiceDesignExecutionTarget(
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

    def _execution(
        self,
        snapshot: SubmitRequest,
        frozen: VoiceDesignRequest,
        policy: VoiceDesignPolicy,
        cancelled: Callable[[], bool],
    ) -> VoiceDesignSynthesis:
        holder: list[VoiceDesignSynthesis] = []

        def check() -> bool:
            dispatched = bool(holder) and holder[0].has_dispatched
            if self._now() >= snapshot.identity.deadline_at:
                raise VoiceDesignCallError(
                    VoiceDesignFailure.DEADLINE_EXCEEDED, request_may_have_run=dispatched
                )
            try:
                self._validate_target(snapshot)
            except ValueError:
                raise VoiceDesignCallError(
                    VoiceDesignFailure.RESPONSE_INVALID, request_may_have_run=dispatched
                ) from None
            return cancelled()

        synthesis = VoiceDesignSynthesis(
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
        return synthesis

    async def synthesize(
        self, request: SubmitRequest, *, cancelled: Callable[[], bool]
    ) -> FileVoiceDesignResult:
        """One bounded synthesis; the caller still owns admission and metering."""
        snapshot, frozen, policy = self._resolve(request)
        remaining = self._remaining(snapshot)
        if remaining <= 0:
            raise VoiceDesignCallError(
                VoiceDesignFailure.DEADLINE_EXCEEDED, request_may_have_run=False
            )
        synthesis = self._execution(snapshot, frozen, policy, cancelled)
        try:
            async with asyncio.timeout(remaining):
                return await synthesis.run()
        except TimeoutError:
            raise VoiceDesignCallError(
                VoiceDesignFailure.DEADLINE_EXCEEDED,
                request_may_have_run=synthesis.has_dispatched,
            ) from None

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: TtsResultStore,
        cancelled: Callable[[], bool],
    ) -> FileStagingOutcome:
        """One synthesis and verified private PUT; never a meter or budget write."""
        snapshot, frozen, policy = self._resolve(request)
        remaining = self._remaining(snapshot)
        if remaining <= 0:
            raise VoiceDesignCallError(
                VoiceDesignFailure.DEADLINE_EXCEEDED, request_may_have_run=False
            )
        synthesis = self._execution(snapshot, frozen, policy, cancelled)
        try:
            async with asyncio.timeout(remaining):
                receipt = await synthesis.stage_result(
                    workspace_id=snapshot.identity.workspace_id,
                    request_digest=snapshot.request_digest,
                    store=store,
                )
        except TimeoutError:
            raise VoiceDesignCallError(
                VoiceDesignFailure.DEADLINE_EXCEEDED,
                request_may_have_run=synthesis.has_dispatched,
            ) from None
        return FileStagingOutcome(
            receipt,
            EngineTerminalResponse(
                snapshot.identity.submit_intent_id, (frozen.request_id,), self._now()
            ),
        )


def _request_bounds(policy: VoiceDesignPolicy, max_output_audio_ms: int) -> tuple[int, int]:
    """Tighten the approved binding to the duration this request declared."""
    bounded = min(max_output_audio_ms, policy.max_audio_ms)
    return bounded, min(policy.max_pcm_bytes, pcm16_mono_bytes(bounded, policy.sample_rate_hz))
