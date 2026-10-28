"""Private deployment activation and composition; no new execution authority."""

from __future__ import annotations

import json
import ssl
import stat
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.asr import AsrPolicy
from speechnexus_gateway.domain.asr_results import AsrResultStore, validate_asr_result_limit
from speechnexus_gateway.domain.execution_inputs import ExecutionInputLimits
from speechnexus_gateway.domain.execution_results import ExecutionStagingResult
from speechnexus_gateway.domain.file_execution import (
    FileExecutionLedger,
    FileExecutionLimits,
    FileStagingOutcome,
)
from speechnexus_gateway.domain.media import AudioDecodeLimits
from speechnexus_gateway.domain.submission import SubmitRequest
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.infrastructure.asr_results_minio import MinioAsrResults
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_gateway.infrastructure.silero_onnx import MAX_VAD_MODEL_BYTES, SileroOnnxCpuModel
from speechnexus_gateway.infrastructure.vllm_asr import (
    VllmAsrEndpoint,
    VllmAsrRecognizer,
    load_controlled_asr_endpoint,
)
from speechnexus_gateway.interfaces.file_asr_command import CommandFileAsr
from speechnexus_gateway.private_deployment import (
    absolute_path,
    explicit_fields,
    nonfinite,
    private_file,
    unique_pairs,
)
from speechnexus_gateway.settings import Settings


class _PrivateConfiguration(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class _Objects(_PrivateConfiguration):
    endpoint: str
    input_bucket: str
    result_bucket: str
    spool_directory: str
    tls_ca_file: str | None
    result_timeout_seconds: float


class _Deployment(_PrivateConfiguration):
    protocol: Literal["file_asr_v1"]
    controlled_endpoint_file: str
    require_correlation_echo: bool = True
    vad_model_file: str
    vad_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    objects: _Objects
    vad_tuning: dict[str, Any]
    input_limits: dict[str, Any]
    decode_limits: dict[str, Any]
    worker_limits: dict[str, Any]
    max_completion_tokens: int
    max_call_seconds: float


@dataclass(frozen=True, slots=True, repr=False)
class FileAsrConfiguration:
    """A startup snapshot; does not create bindings, admit work or attest stop."""

    endpoint: VllmAsrEndpoint
    model_path: Path
    model_sha256: str
    objects: MinioExecutionInputs
    results: MinioAsrResults
    vad: VadPolicy
    inputs: ExecutionInputLimits
    decode: AudioDecodeLimits
    worker: FileExecutionLimits
    max_completion_tokens: int
    max_call_seconds: float

    def execution(self) -> AsrFileStage:
        return AsrFileStage(
            CommandFileAsr(
                inputs=FileExecutionInputs(self.objects),
                decoder=PyAvAudioDecoder(),
                vad_model=SileroOnnxCpuModel(
                    model_path=self.model_path, expected_sha256=self.model_sha256
                ),
                recognizer=VllmAsrRecognizer(self.endpoint),
                vad_tuning=self.vad,
                max_completion_tokens=self.max_completion_tokens,
                max_call_seconds=self.max_call_seconds,
                input_limits=self.inputs,
                decode_limits=self.decode,
            ),
            self.worker.max_result_bytes,
        )

    def build(
        self, ingress: GatewayIngress, ledger: FileExecutionLedger
    ) -> FileExecutionWorker[AsrResultStore]:
        return FileExecutionWorker(
            ingress=ingress,
            ledger=ledger,
            execution=self.execution(),
            results=self.results,
            limits=self.worker,
            engine_request_prefix="file-asr",
        )


@dataclass(frozen=True, slots=True)
class AsrFileStage:
    """Adapt the ASR JSON receipt and its per-call byte budget to the file lane."""

    command: CommandFileAsr
    max_result_bytes: int

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        return self.command.validate_request(request)

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: AsrResultStore,
        cancelled: Callable[[], bool],
    ) -> FileStagingOutcome:
        receipt = await self.command.stage_result(
            request,
            store=store,
            max_result_bytes=self.max_result_bytes,
            cancelled=cancelled,
        )
        return FileStagingOutcome(
            ExecutionStagingResult(
                receipt.request_digest, receipt.reference, receipt.storage_version
            ),
            receipt.terminal,
        )


def load_file_asr_configuration(settings: Settings) -> FileAsrConfiguration | None:
    """Validate before acquiring ingress/DB; no model loading or network requests."""
    path = settings.file_asr_config_file
    if path is None:
        return None
    try:
        deployment = _Deployment.model_validate(
            json.loads(
                private_file(path, 65536),
                object_pairs_hook=unique_pairs,
                parse_constant=nonfinite,
            )
        )
        vad = VadPolicy(
            **explicit_fields(deployment.vad_tuning, {item.name for item in fields(VadPolicy)})
        )
        inputs = ExecutionInputLimits(
            **explicit_fields(
                deployment.input_limits, {item.name for item in fields(ExecutionInputLimits)}
            )
        )
        decode = AudioDecodeLimits(
            **explicit_fields(
                deployment.decode_limits, {item.name for item in fields(AudioDecodeLimits)}
            )
        )
        worker = FileExecutionLimits(
            **explicit_fields(
                deployment.worker_limits, {item.name for item in fields(FileExecutionLimits)}
            )
        )
        validate_asr_result_limit(worker.max_result_bytes)
        AsrPolicy(
            vad.config_revision,
            max_completion_tokens=deployment.max_completion_tokens,
            timeout_seconds=deployment.max_call_seconds,
        )
        endpoint = load_controlled_asr_endpoint(
            absolute_path(deployment.controlled_endpoint_file),
            require_correlation_echo=deployment.require_correlation_echo,
        )
        target = endpoint.execution_target
        if (
            target is None
            or target.host_node_id != settings.node_id
            or target.gateway_id != settings.gateway_id
        ):
            raise ValueError
        model_path = absolute_path(deployment.vad_model_file)
        model_info = model_path.stat()
        if (
            not stat.S_ISREG(model_info.st_mode)
            or not 0 < model_info.st_size <= MAX_VAD_MODEL_BYTES
        ):
            raise ValueError
        ca_file = deployment.objects.tls_ca_file
        context = (
            None
            if ca_file is None
            else ssl.create_default_context(
                cadata=private_file(absolute_path(ca_file), 262144).decode("ascii")
            )
        )
        access_key = settings.file_asr_object_access_key
        secret_key = settings.file_asr_object_secret_key
        if access_key is None or secret_key is None:
            raise ValueError
        objects = MinioExecutionInputs(
            endpoint=deployment.objects.endpoint,
            bucket=deployment.objects.input_bucket,
            access_key=access_key.get_secret_value(),
            secret_key=secret_key.get_secret_value(),
            spool_directory=absolute_path(deployment.objects.spool_directory),
            ssl_context=context,
        )
        results = MinioAsrResults(
            endpoint=deployment.objects.endpoint,
            bucket=deployment.objects.result_bucket,
            access_key=access_key.get_secret_value(),
            secret_key=secret_key.get_secret_value(),
            timeout_seconds=deployment.objects.result_timeout_seconds,
            ssl_context=context,
        )
        return FileAsrConfiguration(
            endpoint,
            model_path,
            deployment.vad_model_sha256,
            objects,
            results,
            vad,
            inputs,
            decode,
            worker,
            deployment.max_completion_tokens,
            deployment.max_call_seconds,
        )
    except Exception:
        raise ValueError("invalid gateway file ASR configuration") from None
