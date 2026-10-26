"""Synthetic private deployment inputs; no real model or service credentials."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from speechnexus_gateway.domain.asr import AsrExecutionTarget
from speechnexus_gateway.domain.execution_inputs import ExecutionInputLimits
from speechnexus_gateway.domain.file_execution import FileExecutionLimits
from speechnexus_gateway.domain.media import AudioDecodeLimits
from speechnexus_gateway.domain.vad import VadPolicy


def write_configuration(
    directory: Path,
    *,
    target: AsrExecutionTarget | None = None,
    policy_revision: str = "policy-file-test",
    port: int = 8000,
    object_endpoint: str = "http://127.0.0.1:9000",
    bucket: str = "synthetic-file-test",
) -> tuple[Path, dict[str, Any]]:
    directory.mkdir(mode=0o700)
    model = directory / "synthetic.onnx"
    model.write_bytes(b"synthetic ONNX placeholder")
    endpoint = directory / "endpoint.json"
    endpoint.write_text(
        json.dumps(
            {
                "protocol": "vllm_api_key_v1",
                "target": asdict(
                    target
                    or AsrExecutionTarget(
                        "node-1", "gateway-1", "asr-batch", "instance-test", 1, "binding-test"
                    )
                ),
                "address": "127.0.0.1",
                "port": port,
                "served_model": "synthetic-asr",
                "bearer_token": "a" * 64,
            }
        )
    )
    endpoint.chmod(0o600)
    document = {
        "protocol": "file_asr_v1",
        "controlled_endpoint_file": str(endpoint),
        "vad_model_file": str(model),
        "vad_model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
        "objects": {
            "endpoint": object_endpoint,
            "input_bucket": bucket,
            "result_bucket": bucket,
            "spool_directory": str(directory),
            "tls_ca_file": None,
            "result_timeout_seconds": 5,
        },
        "vad_tuning": asdict(VadPolicy(policy_revision, max_segment_ms=1000)),
        "input_limits": asdict(ExecutionInputLimits(1000000, 1024, 5)),
        "decode_limits": asdict(AudioDecodeLimits()),
        "worker_limits": asdict(FileExecutionLimits(1024, 16384, 1, 1)),
        "max_completion_tokens": 64,
        "max_call_seconds": 2,
    }
    path = directory / "runtime.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path, document
