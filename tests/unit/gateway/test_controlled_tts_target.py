"""A controlled TTS descriptor binds one private generation and batch pool."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from speechnexus_gateway.domain.tts import TtsExecutionTarget
from speechnexus_gateway.infrastructure.vllm_tts import (
    VllmTtsSynthesizer,
    load_controlled_tts_endpoint,
)

TARGET = TtsExecutionTarget("node-1", "gateway-1", "tts-batch", "instance-1", 1, "binding-1")


def descriptor(path: Path, **overrides: object) -> Path:
    value: dict[str, object] = {
        "protocol": "vllm_api_key_v1",
        "target": asdict(TARGET),
        "address": "127.0.0.1",
        "port": 8001,
        "served_model": "synthetic-tts",
        "bearer_token": "a" * 64,
    }
    value.update(overrides)
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_descriptor_and_admitted_target_must_match(tmp_path: Path) -> None:
    endpoint = load_controlled_tts_endpoint(descriptor(tmp_path / "endpoint.json"))
    synthesizer = VllmTtsSynthesizer(endpoint)
    synthesizer.validate_execution_target(TARGET)
    with pytest.raises(ValueError, match="does not match"):
        synthesizer.validate_execution_target(
            TtsExecutionTarget("node-1", "gateway-1", "tts-batch", "instance-1", 2, "binding-1")
        )


def test_descriptor_cannot_bind_an_asr_pool(tmp_path: Path) -> None:
    target = asdict(TARGET)
    target["pool_id"] = "asr-batch"
    with pytest.raises(ValueError, match="invalid private controlled TTS"):
        load_controlled_tts_endpoint(descriptor(tmp_path / "endpoint.json", target=target))


def test_descriptor_rejects_unknown_target_fields(tmp_path: Path) -> None:
    target = asdict(TARGET)
    target["generation_label"] = "synthetic"
    with pytest.raises(ValueError, match="invalid private controlled TTS"):
        load_controlled_tts_endpoint(descriptor(tmp_path / "endpoint.json", target=target))


def test_descriptor_is_private_and_requires_a_native_credential(tmp_path: Path) -> None:
    path = descriptor(tmp_path / "endpoint.json")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="invalid private controlled TTS"):
        load_controlled_tts_endpoint(path)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="invalid private controlled TTS"):
        load_controlled_tts_endpoint(
            descriptor(tmp_path / "short-token.json", bearer_token="short")
        )
