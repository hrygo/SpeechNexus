"""Synthetic private VoiceDesign deployment inputs; no model or service credentials."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from speechnexus_gateway.domain.file_execution import FileExecutionLimits
from speechnexus_gateway.domain.voice_design import (
    VoiceDesignExecutionTarget,
    VoiceDesignPolicy,
    VoiceDesignTask,
)


def write_voice_design_configuration(
    directory: Path,
    *,
    target: VoiceDesignExecutionTarget | None = None,
    policy: VoiceDesignPolicy | None = None,
    port: int = 8002,
    object_endpoint: str = "http://127.0.0.1:9000",
    bucket: str = "synthetic-voice-design-test",
) -> tuple[Path, dict[str, Any]]:
    directory.mkdir(mode=0o700, exist_ok=True)
    endpoint = directory / "endpoint.json"
    endpoint.write_text(
        json.dumps(
            {
                "protocol": "vllm_api_key_v1",
                "target": asdict(
                    target
                    or VoiceDesignExecutionTarget(
                        "node-1",
                        "gateway-1",
                        "voice-creation",
                        "instance-voice-design",
                        1,
                        "binding-voice-design",
                    )
                ),
                "address": "127.0.0.1",
                "port": port,
                "served_model": "synthetic-voice-design",
                "bearer_token": "a" * 64,
            }
        )
    )
    endpoint.chmod(0o600)
    resolved_policy = policy or VoiceDesignPolicy("policy-voice-design")
    document = {
        "protocol": "file_voice_design_v1",
        "controlled_endpoint_file": str(endpoint),
        "objects": {
            "endpoint": object_endpoint,
            "result_bucket": bucket,
            "tls_ca_file": None,
            "result_timeout_seconds": 5,
        },
        "voice_design_policy": {
            **asdict(resolved_policy),
            "task": VoiceDesignTask.VOICE_DESIGN.value,
            "engine_format": resolved_policy.engine_format.value,
        },
        "worker_limits": asdict(FileExecutionLimits(1024 * 1024, 16384, 1, 1)),
    }
    path = directory / "runtime.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path, document
