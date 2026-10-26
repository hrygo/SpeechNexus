"""Private file-TTS deployment activation; one bounded synthesis per committed command."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, fields
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.file_execution import FileExecutionLedger, FileExecutionLimits
from speechnexus_gateway.domain.tts import (
    TtsEngineFormat,
    TtsPolicy,
    TtsTask,
)
from speechnexus_gateway.domain.tts_results import TtsResultStore
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.infrastructure.tts_results_minio import MinioTtsResults
from speechnexus_gateway.infrastructure.vllm_tts import (
    VllmTtsEndpoint,
    VllmTtsSynthesizer,
    load_controlled_tts_endpoint,
)
from speechnexus_gateway.interfaces.file_tts_command import CommandFileTts
from speechnexus_gateway.private_deployment import (
    absolute_path,
    explicit_fields,
    nonfinite,
    private_file,
    unique_pairs,
)
from speechnexus_gateway.settings import Settings

_MAX_CONFIG_BYTES = 65536
_MAX_CA_BYTES = 262144
_MIN_TTS_PAYLOAD_BYTES = 1024
_MAX_TTS_PAYLOAD_BYTES = 64 * 1024 * 1024


class _PrivateConfiguration(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class _Objects(_PrivateConfiguration):
    endpoint: str
    result_bucket: str
    tls_ca_file: str | None
    result_timeout_seconds: float


class _Deployment(_PrivateConfiguration):
    protocol: Literal["file_tts_v1"]
    controlled_endpoint_file: str
    objects: _Objects
    tts_policy: dict[str, Any]
    worker_limits: dict[str, Any]


@dataclass(frozen=True, slots=True, repr=False)
class FileTtsConfiguration:
    """A startup snapshot; it creates no binding, admission or stop evidence."""

    endpoint: VllmTtsEndpoint
    results: MinioTtsResults
    policy: TtsPolicy
    worker: FileExecutionLimits

    def execution(self) -> CommandFileTts:
        return CommandFileTts(
            synthesizer=VllmTtsSynthesizer(self.endpoint),
            encoder=LocalTtsEncoder(),
            binding_policy=self.policy,
            max_payload_bytes=self.worker.max_result_bytes,
        )

    def build(
        self, ingress: GatewayIngress, ledger: FileExecutionLedger
    ) -> FileExecutionWorker[TtsResultStore]:
        return FileExecutionWorker(
            ingress=ingress,
            ledger=ledger,
            execution=self.execution(),
            results=self.results,
            limits=self.worker,
            engine_request_prefix="file-tts",
        )


def load_file_tts_configuration(settings: Settings) -> FileTtsConfiguration | None:
    """Validate before acquiring ingress/DB; no model loading or network requests."""
    path = settings.file_tts_config_file
    if path is None:
        return None
    try:
        deployment = _Deployment.model_validate(
            json.loads(
                private_file(path, _MAX_CONFIG_BYTES),
                object_pairs_hook=unique_pairs,
                parse_constant=nonfinite,
            )
        )
        worker = FileExecutionLimits(
            **explicit_fields(
                deployment.worker_limits, {item.name for item in fields(FileExecutionLimits)}
            )
        )
        if not _MIN_TTS_PAYLOAD_BYTES <= worker.max_result_bytes <= _MAX_TTS_PAYLOAD_BYTES:
            raise ValueError
        values = explicit_fields(deployment.tts_policy, {item.name for item in fields(TtsPolicy)})
        policy = TtsPolicy(
            **{
                **values,
                "task": TtsTask(values["task"]),
                "engine_format": TtsEngineFormat(values["engine_format"]),
            }
        )
        if policy.sample_rate_hz != 24000:
            raise ValueError
        endpoint = load_controlled_tts_endpoint(absolute_path(deployment.controlled_endpoint_file))
        target = endpoint.execution_target
        if (
            target is None
            or target.host_node_id != settings.node_id
            or target.gateway_id != settings.gateway_id
        ):
            raise ValueError
        ca_file = deployment.objects.tls_ca_file
        context = (
            None
            if ca_file is None
            else ssl.create_default_context(
                cadata=private_file(absolute_path(ca_file), _MAX_CA_BYTES).decode("ascii")
            )
        )
        access_key = settings.file_tts_object_access_key
        secret_key = settings.file_tts_object_secret_key
        if access_key is None or secret_key is None:
            raise ValueError
        results = MinioTtsResults(
            endpoint=deployment.objects.endpoint,
            bucket=deployment.objects.result_bucket,
            access_key=access_key.get_secret_value(),
            secret_key=secret_key.get_secret_value(),
            timeout_seconds=deployment.objects.result_timeout_seconds,
            ssl_context=context,
        )
        return FileTtsConfiguration(endpoint, results, policy, worker)
    except Exception:
        raise ValueError("invalid gateway file TTS configuration") from None
