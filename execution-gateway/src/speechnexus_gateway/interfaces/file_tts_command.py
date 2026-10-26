"""Consume an original file TTS command through the native bounded synthesis pipeline.

This boundary freezes one supported command, performs at most one engine call and
can stage the encoded result privately. It does not admit work, record a terminal
outcome, publish artifacts, or attest engine stop and resource release.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from speechnexus_contracts.execution import parse

from speechnexus_gateway.application.file_tts import FileTtsResult, FileTtsSynthesis
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.execution_results import ExecutionAudioStagingResult
from speechnexus_gateway.domain.submission import (
    MAX_ORIGINAL_COMMAND_BYTES,
    InvalidExecutionRequestError,
    SubmitRequest,
)
from speechnexus_gateway.domain.tts import (
    TtsCallError,
    TtsDeliveryEncoder,
    TtsDeliveryFormat,
    TtsExecutionTarget,
    TtsFailure,
    TtsPolicy,
    TtsRequest,
    TtsSynthesizer,
)
from speechnexus_gateway.domain.tts_results import TtsResultStore

_DELIVERY_FORMATS = {target.value: target for target in TtsDeliveryFormat}


def _invalid(message: str) -> InvalidExecutionRequestError:
    return InvalidExecutionRequestError(message)


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
        max_payload_bytes: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(binding_policy, TtsPolicy):
            raise ValueError("an approved TTS binding policy is required")
        if type(max_payload_bytes) is not int or not 1024 <= max_payload_bytes <= 64 * 1024 * 1024:
            raise ValueError("TTS delivery limit is out of range")
        self._synthesizer = synthesizer
        self._encoder = encoder
        self._binding_policy = binding_policy
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
                or identity.voice_revision_id is None
            ):
                raise ValueError
            text = command["text"]
            if not isinstance(text, str) or command["text_stream"] is not None:
                raise ValueError
            parameters = command["parameters"]
            planner = parameters["planner"]
            if (
                parameters["text_commit_mode"] != "explicit"
                or parameters["audio_delivery"] != "complete"
                or parameters["pronunciation_revision"] is not None
                or not isinstance(planner, dict)
                or not 1 <= len(text) <= planner["max_chars"]
            ):
                raise ValueError
            delivery = _DELIVERY_FORMATS[parameters["response_format"]]
            policy = replace(
                self._binding_policy,
                instructions=parameters["instructions"] or "",
                speed=parameters["speed"],
                language=parameters["language"],
                seed=parameters["seed"],
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
    ) -> ExecutionAudioStagingResult:
        """One synthesis and verified private PUT; never a terminal or meter write."""
        snapshot, frozen, policy, delivery = self._resolve(request)
        remaining = self._remaining(snapshot)
        if remaining <= 0:
            raise TtsCallError(TtsFailure.DEADLINE_EXCEEDED, request_may_have_run=False)
        synthesis = self._execution(snapshot, frozen, policy, delivery, cancelled)
        try:
            async with asyncio.timeout(remaining):
                return await synthesis.stage_result(
                    workspace_id=snapshot.identity.workspace_id,
                    request_digest=snapshot.request_digest,
                    store=store,
                )
        except TimeoutError:
            raise TtsCallError(
                TtsFailure.DEADLINE_EXCEEDED, request_may_have_run=synthesis.has_dispatched
            ) from None
