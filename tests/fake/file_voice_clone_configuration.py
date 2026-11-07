"""Synthetic private Base voice-clone deployment inputs; no model or credentials."""

import json
import math
import struct
from dataclasses import asdict
from pathlib import Path
from typing import Any

from speechnexus_gateway.domain.execution_inputs import ExecutionInputLimits
from speechnexus_gateway.domain.file_execution import FileExecutionLimits
from speechnexus_gateway.domain.voice_clone import (
    VoiceCloneExecutionTarget,
    VoiceClonePolicy,
    VoiceCloneTask,
)

REFERENCE_TEXT = "Synthetic warmup reference."


def reference_wav(*, duration_ms: int = 1500, rate: int = 16000, amplitude: int = 4096) -> bytes:
    """One small mono 16-bit PCM tone; a structural fixture, not speech."""

    frames = bytearray()
    total = rate * duration_ms // 1000
    for index in range(total):
        sample = int(amplitude * math.sin(2 * math.pi * 220 * index / rate))
        frames += struct.pack("<h", sample)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(frames))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(frames))
        + bytes(frames)
    )


def write_voice_clone_configuration(
    directory: Path,
    *,
    target: VoiceCloneExecutionTarget | None = None,
    policy: VoiceClonePolicy | None = None,
    port: int = 8004,
    object_endpoint: str = "http://127.0.0.1:9000",
    input_bucket: str = "synthetic-voice-clone-test",
    result_bucket: str = "synthetic-voice-clone-results",
    readiness: dict[str, Any] | None = None,
    reference: bytes | None = None,
    reference_text: str = REFERENCE_TEXT,
) -> tuple[Path, dict[str, Any]]:
    directory.mkdir(mode=0o700, exist_ok=True)
    spool = directory / "spool"
    spool.mkdir(mode=0o700, exist_ok=True)
    spool.chmod(0o700)
    endpoint = directory / "endpoint.json"
    endpoint.write_text(
        json.dumps(
            {
                "protocol": "vllm_api_key_v1",
                "target": asdict(
                    target
                    or VoiceCloneExecutionTarget(
                        "node-1",
                        "gateway-1",
                        "voice-creation",
                        "instance-voice-clone",
                        1,
                        "binding-voice-clone",
                    )
                ),
                "address": "127.0.0.1",
                "port": port,
                "served_model": "synthetic-voice-clone",
                "bearer_token": "a" * 64,
            }
        )
    )
    endpoint.chmod(0o600)
    reference_path = directory / "warmup-reference.wav"
    reference_path.write_bytes(reference if reference is not None else reference_wav())
    reference_path.chmod(0o600)
    resolved_policy = policy or VoiceClonePolicy("policy-voice-clone")
    document = {
        "protocol": "file_voice_clone_v1",
        "controlled_endpoint_file": str(endpoint),
        "objects": {
            "endpoint": object_endpoint,
            "input_bucket": input_bucket,
            "result_bucket": result_bucket,
            "spool_directory": str(spool),
            "tls_ca_file": None,
            "result_timeout_seconds": 5,
        },
        "voice_clone_policy": {
            "config_revision": resolved_policy.config_revision,
            "task": VoiceCloneTask.BASE.value,
            "engine_format": resolved_policy.engine_format.value,
            "sample_rate_hz": resolved_policy.sample_rate_hz,
            "max_pcm_bytes": resolved_policy.max_pcm_bytes,
            "max_audio_ms": resolved_policy.max_audio_ms,
            "timeout_seconds": resolved_policy.timeout_seconds,
            "supported_languages": list(resolved_policy.supported_languages),
            "reference_sample_rates": list(resolved_policy.reference_sample_rates),
            "min_reference_audio_ms": resolved_policy.min_reference_audio_ms,
            "max_reference_audio_ms": resolved_policy.max_reference_audio_ms,
            "max_reference_audio_bytes": resolved_policy.max_reference_audio_bytes,
            "allow_x_vector_only": resolved_policy.allow_x_vector_only,
        },
        "input_limits": asdict(ExecutionInputLimits(8 * 1024 * 1024, 1024 * 1024, 5)),
        "worker_limits": asdict(FileExecutionLimits(1024 * 1024, 16384, 1, 1)),
        "warmup": {
            "reference_audio_file": str(reference_path),
            "reference_text": reference_text,
            "preview_text": "Ready.",
            "language": "en",
        },
    }
    if readiness is not None:
        document["readiness"] = readiness
    path = directory / "runtime.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path, document


def write_reference_wav(path: Path, *, duration_ms: int = 1500, rate: int = 16000) -> Path:
    path.write_bytes(reference_wav(duration_ms=duration_ms, rate=rate))
    path.chmod(0o600)
    return path


__all__ = [
    "REFERENCE_TEXT",
    "reference_wav",
    "write_reference_wav",
    "write_voice_clone_configuration",
]
