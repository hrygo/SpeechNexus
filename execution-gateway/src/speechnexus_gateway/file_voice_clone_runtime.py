"""Private Base voice-clone deployment activation; one bounded clone per committed command."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field, fields, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from speechnexus_gateway.application.engine_warmup import (
    DEFAULT_RETRY_INTERVAL_SECONDS,
    DEFAULT_WARMUP_TIMEOUT_SECONDS,
    MAX_WARMUP_TIMEOUT_SECONDS,
)
from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.voice_clone_readiness import VoiceCloneWarmupProbe
from speechnexus_gateway.domain.execution_inputs import ExecutionInputLimits
from speechnexus_gateway.domain.file_execution import FileExecutionLedger, FileExecutionLimits
from speechnexus_gateway.domain.tts import TtsEngineFormat
from speechnexus_gateway.domain.tts_results import TtsResultStore
from speechnexus_gateway.domain.voice_clone import (
    VOICE_CLONE_SAMPLE_RATE_HZ,
    ReferenceAudio,
    VoiceClonePolicy,
    VoiceCloneRequest,
    VoiceCloneTask,
)
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.infrastructure.tts_results_minio import MinioTtsResults
from speechnexus_gateway.infrastructure.vllm_voice_clone import (
    VllmVoiceCloneEndpoint,
    VllmVoiceCloneSynthesizer,
    load_controlled_voice_clone_endpoint,
)
from speechnexus_gateway.interfaces.file_voice_clone_command import CommandFileVoiceClone
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
_MIN_VOICE_CLONE_PAYLOAD_BYTES = 1024
_MAX_VOICE_CLONE_PAYLOAD_BYTES = 64 * 1024 * 1024
_WARMUP_CONFIG_REVISION = "voice-clone-warmup"


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


class _Warmup(_PrivateConfiguration):
    reference_audio_file: str
    reference_text: str = Field(min_length=1, max_length=10000)
    preview_text: str = Field(default="Ready.", min_length=1, max_length=2000)
    language: str = Field(default="en", min_length=2, max_length=35)


class _Readiness(_PrivateConfiguration):
    warmup_timeout_seconds: float = DEFAULT_WARMUP_TIMEOUT_SECONDS
    retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS


class _Deployment(_PrivateConfiguration):
    protocol: Literal["file_voice_clone_v1"]
    controlled_endpoint_file: str
    objects: _Objects
    voice_clone_policy: dict[str, Any]
    input_limits: dict[str, Any]
    worker_limits: dict[str, Any]
    warmup: _Warmup
    readiness: _Readiness = Field(default_factory=_Readiness)


@dataclass(frozen=True, slots=True, repr=False)
class VoiceCloneConfiguration:
    """A startup snapshot; it creates no binding, admission or stop evidence."""

    endpoint: VllmVoiceCloneEndpoint
    objects: MinioExecutionInputs
    results: MinioTtsResults
    policy: VoiceClonePolicy
    inputs: ExecutionInputLimits
    worker: FileExecutionLimits
    warmup_reference: ReferenceAudio = field(repr=False)
    warmup_reference_text: str = field(repr=False)
    warmup_preview_text: str = "Ready."
    warmup_language: str = "en"
    warmup_timeout_seconds: float = DEFAULT_WARMUP_TIMEOUT_SECONDS
    retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS

    def execution(self) -> CommandFileVoiceClone:
        return CommandFileVoiceClone(
            inputs=FileExecutionInputs(self.objects),
            synthesizer=VllmVoiceCloneSynthesizer(self.endpoint),
            encoder=LocalTtsEncoder(),
            binding_policy=self.policy,
            input_limits=self.inputs,
            max_payload_bytes=self.worker.max_result_bytes,
        )

    def warmup_probe(self) -> VoiceCloneWarmupProbe:
        policy = replace(
            self.policy,
            config_revision=_WARMUP_CONFIG_REVISION,
            timeout_seconds=min(self.warmup_timeout_seconds, 600.0),
        )
        request = VoiceCloneRequest(
            _WARMUP_CONFIG_REVISION,
            self.warmup_preview_text,
            self.warmup_language,
            self.warmup_reference,
            self.warmup_reference_text,
        )
        return VoiceCloneWarmupProbe(
            VllmVoiceCloneSynthesizer(self.endpoint, warmup_request=request, warmup_policy=policy),
            timeout_seconds=self.warmup_timeout_seconds,
            retry_interval_seconds=self.retry_interval_seconds,
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
            engine_request_prefix="file-voice-clone",
        )


def load_voice_clone_configuration(settings: Settings) -> VoiceCloneConfiguration | None:
    """Validate before acquiring ingress/DB; no model loading or network requests."""
    path = settings.file_voice_clone_config_file
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
        if not (
            _MIN_VOICE_CLONE_PAYLOAD_BYTES
            <= worker.max_result_bytes
            <= _MAX_VOICE_CLONE_PAYLOAD_BYTES
        ):
            raise ValueError
        inputs = ExecutionInputLimits(
            **explicit_fields(
                deployment.input_limits, {item.name for item in fields(ExecutionInputLimits)}
            )
        )
        values = explicit_fields(
            deployment.voice_clone_policy, {item.name for item in fields(VoiceClonePolicy)}
        )
        for name in ("supported_languages", "reference_sample_rates"):
            if name in values:
                sequence = values[name]
                if not isinstance(sequence, list) or not all(
                    isinstance(item, str if name == "supported_languages" else int)
                    for item in sequence
                ):
                    raise ValueError
                values[name] = tuple(sequence)
        policy = VoiceClonePolicy(
            **{
                **values,
                "task": VoiceCloneTask(values["task"]),
                "engine_format": TtsEngineFormat(values["engine_format"]),
            }
        )
        if policy.sample_rate_hz != VOICE_CLONE_SAMPLE_RATE_HZ:
            raise ValueError
        if inputs.max_bytes > policy.max_reference_audio_bytes:
            raise ValueError
        endpoint = load_controlled_voice_clone_endpoint(
            absolute_path(deployment.controlled_endpoint_file)
        )
        target = endpoint.execution_target
        if (
            target is None
            or target.host_node_id != settings.node_id
            or target.gateway_id != settings.gateway_id
        ):
            raise ValueError
        warmup = deployment.warmup
        if (
            warmup.language.split("-", 1)[0].lower() not in policy.supported_languages
            or warmup.language == "auto"
        ):
            raise ValueError
        reference = ReferenceAudio.from_wav(
            private_file(
                absolute_path(warmup.reference_audio_file), policy.max_reference_audio_bytes
            ),
            sample_rates=policy.reference_sample_rates,
            min_duration_ms=policy.min_reference_audio_ms,
            max_duration_ms=policy.max_reference_audio_ms,
            max_bytes=policy.max_reference_audio_bytes,
        )
        # Prove the approved pairing now, so a bad warmup fails startup instead of
        # silently leaving the lane permanently unready.
        VoiceCloneRequest(
            _WARMUP_CONFIG_REVISION,
            warmup.preview_text,
            warmup.language,
            reference,
            warmup.reference_text,
        )
        if not (
            1.0 <= deployment.readiness.warmup_timeout_seconds <= MAX_WARMUP_TIMEOUT_SECONDS
            and 0.0 <= deployment.readiness.retry_interval_seconds <= 600.0
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
        access_key = settings.file_voice_clone_object_access_key
        secret_key = settings.file_voice_clone_object_secret_key
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
        results = MinioTtsResults(
            endpoint=deployment.objects.endpoint,
            bucket=deployment.objects.result_bucket,
            access_key=access_key.get_secret_value(),
            secret_key=secret_key.get_secret_value(),
            timeout_seconds=deployment.objects.result_timeout_seconds,
            ssl_context=context,
        )
        return VoiceCloneConfiguration(
            endpoint,
            objects,
            results,
            policy,
            inputs,
            worker,
            reference,
            warmup.reference_text,
            warmup.preview_text,
            warmup.language,
            deployment.readiness.warmup_timeout_seconds,
            deployment.readiness.retry_interval_seconds,
        )
    except Exception:
        raise ValueError("invalid gateway voice clone configuration") from None
