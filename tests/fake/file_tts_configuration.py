"""Synthetic private file-TTS deployment inputs; no model or service credentials."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from speechnexus_gateway.domain.file_execution import FileExecutionLimits
from speechnexus_gateway.domain.tts import TtsExecutionTarget, TtsPolicy


def write_tts_configuration(
    directory: Path,
    *,
    target: TtsExecutionTarget | None = None,
    policy: TtsPolicy | None = None,
    port: int = 8001,
    object_endpoint: str = "http://127.0.0.1:9000",
    bucket: str = "synthetic-tts-test",
) -> tuple[Path, dict[str, Any]]:
    directory.mkdir(mode=0o700)
    endpoint = directory / "endpoint.json"
    endpoint.write_text(
        json.dumps(
            {
                "protocol": "vllm_api_key_v1",
                "target": asdict(
                    target
                    or TtsExecutionTarget(
                        "node-1", "gateway-1", "tts-batch", "instance-tts", 1, "binding-tts"
                    )
                ),
                "address": "127.0.0.1",
                "port": port,
                "served_model": "synthetic-tts",
                "bearer_token": "a" * 64,
            }
        )
    )
    endpoint.chmod(0o600)
    document = {
        "protocol": "file_tts_v1",
        "controlled_endpoint_file": str(endpoint),
        "objects": {
            "endpoint": object_endpoint,
            "result_bucket": bucket,
            "tls_ca_file": None,
            "result_timeout_seconds": 5,
        },
        "tts_policy": asdict(policy or TtsPolicy("policy-tts", "Vivian")),
        "worker_limits": asdict(FileExecutionLimits(1024 * 1024, 16384, 1, 1)),
    }
    path = directory / "runtime.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path, document
