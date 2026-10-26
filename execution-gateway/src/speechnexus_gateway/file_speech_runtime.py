"""Compose configured file ASR and TTS lanes behind one persisted-command worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.asr_results import AsrResultStore
from speechnexus_gateway.domain.canonical import JsonValue
from speechnexus_gateway.domain.execution_results import StagingResult
from speechnexus_gateway.domain.file_execution import FileExecutionLedger, FileExecutionLimits
from speechnexus_gateway.domain.submission import InvalidExecutionRequestError, SubmitRequest
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
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from speechnexus_gateway.interfaces.file_tts_command import CommandFileTts
from speechnexus_gateway.settings import Settings


@dataclass(frozen=True, slots=True)
class _Stores:
    asr: AsrResultStore | None
    tts: TtsResultStore | None


def _kind(request: SubmitRequest) -> str:
    command: JsonValue = request.command
    if not isinstance(command, dict):
        raise InvalidExecutionRequestError("file command is not an object")
    kind = command.get("kind")
    if kind not in {"asr", "tts"}:
        raise InvalidExecutionRequestError("file command kind is not supported")
    return kind


@dataclass(frozen=True, slots=True)
class _RoutedExecution:
    asr: AsrFileStage | None
    tts: CommandFileTts | None

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        kind = _kind(request)
        if kind == "asr" and self.asr is not None:
            return self.asr.validate_request(request)
        if kind == "tts" and self.tts is not None:
            return self.tts.validate_request(request)
        raise InvalidExecutionRequestError("the configured gateway does not serve this file stage")

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: _Stores,
        cancelled: Callable[[], bool],
    ) -> StagingResult:
        kind = _kind(request)
        if kind == "asr" and self.asr is not None and store.asr is not None:
            return await self.asr.stage_result(request, store=store.asr, cancelled=cancelled)
        if kind == "tts" and self.tts is not None and store.tts is not None:
            return await self.tts.stage_result(request, store=store.tts, cancelled=cancelled)
        raise InvalidExecutionRequestError("the configured file result store is unavailable")


@dataclass(frozen=True, slots=True)
class FileSpeechConfiguration:
    asr: FileAsrConfiguration | None
    tts: FileTtsConfiguration | None
    limits: FileExecutionLimits

    def build(
        self, ingress: GatewayIngress, ledger: FileExecutionLedger
    ) -> FileExecutionWorker[_Stores]:
        stores = _Stores(
            None if self.asr is None else self.asr.results,
            None if self.tts is None else self.tts.results,
        )
        execution = _RoutedExecution(
            None if self.asr is None else self.asr.execution(),
            None if self.tts is None else self.tts.execution(),
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
    if asr is None and tts is None:
        return None
    limits = asr.worker if asr is not None else tts.worker if tts is not None else None
    if limits is None:
        return None
    if asr is not None and tts is not None:
        shared = (
            asr.worker.max_command_bytes == tts.worker.max_command_bytes,
            asr.worker.max_pending == tts.worker.max_pending,
            asr.worker.shutdown_seconds == tts.worker.shutdown_seconds,
        )
        if not all(shared):
            raise ValueError("file ASR and TTS must share command, pending and shutdown limits")
    return FileSpeechConfiguration(asr, tts, limits)
