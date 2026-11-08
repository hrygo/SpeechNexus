"""Compose configured file ASR and resident TTS variants behind one persisted-command worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.asr_results import AsrResultStore
from speechnexus_gateway.domain.canonical import JsonValue
from speechnexus_gateway.domain.file_execution import (
    FileCommandInvalidError,
    FileExecutionLedger,
    FileExecutionLimits,
    FileStageUnavailableError,
    FileStagingOutcome,
)
from speechnexus_gateway.domain.submission import SubmitRequest
from speechnexus_gateway.domain.tts_results import TtsResultStore
from speechnexus_gateway.file_asr_runtime import (
    AsrFileStage,
    FileAsrConfiguration,
    load_file_asr_configuration,
)
from speechnexus_gateway.file_tts_runtime import (
    FileTtsConfiguration,
    load_file_tts_configuration,
)
from speechnexus_gateway.file_voice_clone_runtime import (
    VoiceCloneConfiguration,
    load_voice_clone_configuration,
)
from speechnexus_gateway.file_voice_design_runtime import (
    VoiceDesignConfiguration,
    load_voice_design_configuration,
)
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from speechnexus_gateway.interfaces.file_tts_command import CommandFileCustomTts, CommandFileTts
from speechnexus_gateway.interfaces.file_voice_clone_command import CommandFileVoiceClone
from speechnexus_gateway.interfaces.file_voice_design_command import CommandFileVoiceDesign
from speechnexus_gateway.settings import Settings


@dataclass(frozen=True, slots=True)
class _Stores:
    asr: AsrResultStore | None
    tts: TtsResultStore | None
    voice_design: TtsResultStore | None
    voice_clone: TtsResultStore | None = None


def _kind(request: SubmitRequest) -> str:
    command: JsonValue = request.command
    if not isinstance(command, dict):
        raise FileCommandInvalidError("file command is not an object")
    kind = command.get("kind")
    if kind not in {"asr", "tts", "voicedesign", "voiceclone"}:
        raise FileCommandInvalidError("file command kind is not supported")
    return kind


def _custom_voice(request: SubmitRequest) -> bool:
    command = request.command
    assert isinstance(command, dict)
    resources = command.get("resources")
    return isinstance(resources, dict) and resources.get("voice_kind") == "custom"


@dataclass(frozen=True, slots=True)
class _RoutedExecution:
    asr: AsrFileStage | None
    tts: CommandFileTts | None
    voice_design: CommandFileVoiceDesign | None
    voice_clone: CommandFileVoiceClone | None = None
    custom_tts: CommandFileCustomTts | None = None

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        kind = _kind(request)
        if kind == "asr" and self.asr is not None:
            return self.asr.validate_request(request)
        if kind == "tts":
            if _custom_voice(request):
                if self.custom_tts is not None:
                    return self.custom_tts.validate_request(request)
            elif self.tts is not None:
                # The builtin lane never accepts custom material; it does not
                # silently fall back to the configured native speaker.
                return self.tts.validate_request(request)
        if kind == "voicedesign" and self.voice_design is not None:
            return self.voice_design.validate_request(request)
        if kind == "voiceclone" and self.voice_clone is not None:
            return self.voice_clone.validate_request(request)
        # Known semantic deviation: an unconfigured stage is a deployment or
        # routing mismatch (an operator fact), but the only deterministic code
        # this boundary owns is the caller-facing invalid-request code. Mapping
        # it to a transport-unknown terminal would be worse, so it stays here
        # until a dedicated execution error code exists.
        raise FileStageUnavailableError("the configured gateway does not serve this file stage")

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: _Stores,
        cancelled: Callable[[], bool],
    ) -> FileStagingOutcome:
        kind = _kind(request)
        if kind == "asr" and self.asr is not None and store.asr is not None:
            return await self.asr.stage_result(request, store=store.asr, cancelled=cancelled)
        if kind == "tts":
            if _custom_voice(request):
                if self.custom_tts is not None and store.voice_clone is not None:
                    return await self.custom_tts.stage_result(
                        request, store=store.voice_clone, cancelled=cancelled
                    )
            elif self.tts is not None and store.tts is not None:
                return await self.tts.stage_result(request, store=store.tts, cancelled=cancelled)
        if (
            kind == "voicedesign"
            and self.voice_design is not None
            and store.voice_design is not None
        ):
            return await self.voice_design.stage_result(
                request, store=store.voice_design, cancelled=cancelled
            )
        if kind == "voiceclone" and self.voice_clone is not None and store.voice_clone is not None:
            return await self.voice_clone.stage_result(
                request, store=store.voice_clone, cancelled=cancelled
            )
        raise FileStageUnavailableError("the configured file result store is unavailable")


@dataclass(frozen=True, slots=True)
class FileSpeechConfiguration:
    asr: FileAsrConfiguration | None
    tts: FileTtsConfiguration | None
    voice_design: VoiceDesignConfiguration | None
    limits: FileExecutionLimits
    voice_clone: VoiceCloneConfiguration | None = None

    def build(
        self, ingress: GatewayIngress, ledger: FileExecutionLedger
    ) -> FileExecutionWorker[_Stores]:
        stores = _Stores(
            None if self.asr is None else self.asr.results,
            None if self.tts is None else self.tts.results,
            None if self.voice_design is None else self.voice_design.results,
            None if self.voice_clone is None else self.voice_clone.results,
        )
        execution = _RoutedExecution(
            None if self.asr is None else self.asr.execution(),
            None if self.tts is None else self.tts.execution(),
            None if self.voice_design is None else self.voice_design.execution(),
            None if self.voice_clone is None else self.voice_clone.execution(),
            None if self.voice_clone is None else self.voice_clone.custom_tts(),
        )
        return FileExecutionWorker(
            ingress=ingress,
            ledger=ledger,
            execution=execution,
            results=stores,
            limits=self.limits,
            engine_request_prefix="file-command",
        )


def load_file_speech_configuration(settings: Settings) -> FileSpeechConfiguration | None:
    asr = load_file_asr_configuration(settings)
    tts = load_file_tts_configuration(settings)
    voice_design = load_voice_design_configuration(settings)
    voice_clone = load_voice_clone_configuration(settings)
    if asr is None and tts is None and voice_design is None and voice_clone is None:
        return None
    # One node keeps one resident TTS variant: VoiceDesign, Base clone and
    # CustomVoice are separate checkpoint/task loads that exhaust the GPU when
    # combined. Switching variants is a stop-then-prepare deployment action.
    variants = [lane for lane in (tts, voice_design, voice_clone) if lane is not None]
    if len(variants) > 1:
        raise ValueError("a gateway cannot serve more than one resident TTS variant at a time")
    lanes = [lane for lane in (asr, tts, voice_design, voice_clone) if lane is not None]
    limits = lanes[0].worker
    shared = (
        all(lane.worker.max_command_bytes == limits.max_command_bytes for lane in lanes),
        all(lane.worker.max_pending == limits.max_pending for lane in lanes),
        all(lane.worker.shutdown_seconds == limits.shutdown_seconds for lane in lanes),
    )
    if not all(shared):
        raise ValueError("file speech lanes must share command, pending and shutdown limits")
    return FileSpeechConfiguration(asr, tts, voice_design, limits, voice_clone)
