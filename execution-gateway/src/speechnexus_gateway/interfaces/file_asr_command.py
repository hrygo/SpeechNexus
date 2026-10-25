"""Consume an original file command through the native bounded ASR pipeline.

This boundary converts the existing machine contract; it does not admit work,
acknowledge dispatch, publish results, or attest engine stop/resource release.
The execution owner must persist and authorize the execution before using it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from speechnexus_contracts.execution import parse

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.application.file_asr import FileAsrTranscription
from speechnexus_gateway.application.file_asr_audio import FileAsrAudio
from speechnexus_gateway.application.file_asr_result import stage_file_asr_result
from speechnexus_gateway.domain.asr import (
    AsrCallError,
    AsrFailure,
    AsrPolicy,
    AsrRecognizer,
)
from speechnexus_gateway.domain.asr_results import (
    AsrResultStore,
    StagedAsrResult,
    validate_asr_result_limit,
)
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.execution_inputs import ExecutionInputLimits
from speechnexus_gateway.domain.media import AudioDecodeLimits, AudioDecoder
from speechnexus_gateway.domain.submission import (
    MAX_ORIGINAL_COMMAND_BYTES,
    InvalidExecutionRequestError,
    SubmitRequest,
)
from speechnexus_gateway.domain.vad import VadModel, VadPolicy


class CommandFileAsr:
    """A single-use transcription per open, with no independent policy defaults.

    Token/call/media bounds and VAD acoustic tuning come from the approved
    deployment. Command values override only their corresponding native policy
    fields; unsupported requested capabilities are rejected before object I/O.
    """

    def __init__(
        self,
        *,
        inputs: FileExecutionInputs,
        decoder: AudioDecoder,
        vad_model: VadModel,
        recognizer: AsrRecognizer,
        vad_tuning: VadPolicy,
        max_completion_tokens: int,
        max_call_seconds: float,
        input_limits: ExecutionInputLimits,
        decode_limits: AudioDecodeLimits,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        # Reuse native validation; these are local consumption bounds, not quota.
        AsrPolicy(
            vad_tuning.config_revision,
            max_completion_tokens=max_completion_tokens,
            timeout_seconds=max_call_seconds,
        )
        self._inputs = inputs
        self._decoder = decoder
        self._vad_model = vad_model
        self._recognizer = recognizer
        self._vad_tuning = vad_tuning
        self._max_tokens = max_completion_tokens
        self._max_call_seconds = max_call_seconds
        self._input_limits = input_limits
        self._decode_limits = decode_limits
        self._now = now
        self._monotonic = monotonic

    def _resolve(self, request: SubmitRequest) -> tuple[SubmitRequest, AsrPolicy, VadPolicy, float]:
        try:
            # Caller mutations after preparation cannot change this execution.
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
                command["kind"] != "asr"
                or command["task_kind"] != "asr_file"
                or command["stage_role"] != "asr_batch"
            ):
                raise ValueError
            if (
                snapshot.identity.binding_revision_id != self._recognizer.binding_revision_id
                or snapshot.identity.policy_revision_id != self._vad_tuning.config_revision
                or snapshot.identity.voice_revision_id is not None
                or snapshot.output_stream_id is not None
            ):
                raise ValueError
            parameters = command["parameters"]
            if (
                parameters["turn_detection"]["mode"] != "server_vad"
                or parameters["finalization"] != "full_segment"
                or parameters["rollback_tokens"] != 0
                or parameters["max_segment_ms"] < parameters["preview_interval_ms"]
                or parameters["alignment"] != "off"
                or parameters["diarization"] != "off"
                or parameters["vocabulary_set_revision"] is not None
                or parameters["timestamp_granularities"]
                or parameters["response_format"] not in ("json", "text")
                or command["resources"]
                != {
                    "voice": None,
                    "voice_material": None,
                    "vocabulary": [],
                    "pronunciation": [],
                    "voice_kind": None,
                    "vocabulary_source": None,
                    "pronunciation_source": None,
                }
            ):
                raise ValueError
            policy = AsrPolicy(
                snapshot.identity.policy_revision_id,
                language=parameters["language"],
                prompt=parameters["prompt"] if parameters["prompt"] is not None else "",
                temperature=parameters["temperature"],
                max_completion_tokens=self._max_tokens,
                timeout_seconds=min(self._max_call_seconds, parameters["final_deadline_ms"] / 1000),
            )
            vad = replace(
                self._vad_tuning,
                silence_duration_ms=parameters["turn_detection"]["silence_duration_ms"],
                max_segment_ms=parameters["max_segment_ms"],
            )
        except ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError:
            raise InvalidExecutionRequestError(
                "file ASR command, capability or configured binding does not match"
            ) from None
        return snapshot, policy, vad, parameters["final_deadline_ms"] / 1000

    def _execution_cancelled(
        self,
        request: SubmitRequest,
        *,
        cancelled: Callable[[], bool],
        request_may_have_run: bool,
    ) -> bool:
        if self._now() >= request.identity.deadline_at:
            raise AsrCallError(
                AsrFailure.DEADLINE_EXCEEDED, request_may_have_run=request_may_have_run
            )
        if self._recognizer.binding_revision_id != request.identity.binding_revision_id:
            raise AsrCallError(
                AsrFailure.RESPONSE_INVALID, request_may_have_run=request_may_have_run
            )
        return cancelled()

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: AsrResultStore,
        max_result_bytes: int,
        cancelled: Callable[[], bool],
    ) -> StagedAsrResult:
        """Stage a complete private result outside the short submit handshake.

        This receipt neither closes the execution nor proves stop/release. The
        execution owner must durably record it and hand it to A's publisher.
        """
        validate_asr_result_limit(max_result_bytes)
        snapshot, _, _, _ = self._resolve(request)
        async with self.open(snapshot, cancelled=cancelled) as run:

            def check() -> None:
                if self._execution_cancelled(
                    snapshot, cancelled=cancelled, request_may_have_run=run.has_dispatched
                ):
                    raise AsrCallError(
                        AsrFailure.CANCELLED, request_may_have_run=run.has_dispatched
                    )

            check()
            remaining = (snapshot.identity.deadline_at - self._now()).total_seconds()
            try:
                async with asyncio.timeout(remaining):
                    return await stage_file_asr_result(
                        snapshot, run, store=store, max_result_bytes=max_result_bytes, check=check
                    )
            except TimeoutError:
                raise AsrCallError(
                    AsrFailure.DEADLINE_EXCEEDED, request_may_have_run=run.has_dispatched
                ) from None

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        """Freeze the supported command before accepting work; no external I/O."""
        snapshot, _, _, _ = self._resolve(request)
        return snapshot

    @asynccontextmanager
    async def open(
        self, request: SubmitRequest, *, cancelled: Callable[[], bool]
    ) -> AsyncIterator[FileAsrTranscription]:
        """Prepare before I/O, then close media and the private input on every exit.

        The caller owns the persisted execution lifecycle. In particular this
        long-running iterator must not run inside the short ingress handshake.
        """
        snapshot, policy, vad, final_deadline = self._resolve(request)
        transcription: FileAsrTranscription | None = None

        def should_cancel() -> bool:
            may_have_run = transcription is not None and transcription.has_dispatched
            return self._execution_cancelled(
                snapshot, cancelled=cancelled, request_may_have_run=may_have_run
            )

        if should_cancel():
            raise AsrCallError(AsrFailure.CANCELLED, request_may_have_run=False)
        async with self._inputs.open_asr(
            snapshot, limits=self._input_limits, cancelled=should_cancel
        ) as prepared:
            if should_cancel():
                raise AsrCallError(AsrFailure.CANCELLED, request_may_have_run=False)
            audio = FileAsrAudio(
                decoder=self._decoder,
                model=self._vad_model,
                source=prepared.object_input.stream,
                source_format=prepared.source_format,
                limits=self._decode_limits,
                vad_policy=vad,
                cancelled=should_cancel,
                monotonic=self._monotonic,
            )
            try:
                transcription = FileAsrTranscription(
                    audio=audio,
                    recognizer=self._recognizer,
                    execution_id=snapshot.identity.submit_intent_id,
                    policy=policy,
                    cancelled=should_cancel,
                    final_deadline_seconds=final_deadline,
                    monotonic=self._monotonic,
                )
                yield transcription
            finally:
                if transcription is not None:
                    await transcription.aclose()
                else:
                    audio.close()
