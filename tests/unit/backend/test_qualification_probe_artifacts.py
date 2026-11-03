"""The offline qualification check must consume verified, fixed artifact bytes."""

import hashlib
from typing import Any

import pytest

from speechnexus_backend.qualification_probe_artifacts import (
    ArtifactQualificationError,
    verify_local_tts_artifacts,
)
from tests.unit.test_file_tts_artifacts import tts_inputs as tts_inputs


def _verify(inputs: dict[str, Any], digest: str) -> Any:
    return verify_local_tts_artifacts(
        pin_path=inputs["pin_path"],
        model_root=inputs["model_root"],
        repository_root=inputs["repository_root"],
        expected_pin_digest=digest,
    )


def test_checks_all_local_bytes_against_independently_fixed_pin(tts_inputs: dict[str, Any]) -> None:
    digest = "sha256:" + hashlib.sha256(tts_inputs["pin_path"].read_bytes()).hexdigest()
    result = _verify(tts_inputs, digest)
    assert result.artifact_count == 11
    assert result.model_id == "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    assert str(tts_inputs["model_root"]) not in repr(result)
    with pytest.raises(ArtifactQualificationError, match="artifact_integrity_unverified"):
        _verify(tts_inputs, "sha256:" + "0" * 64)
    file_path = tts_inputs["model_root"] / tts_inputs["pin"]["model"]["files"][0]["file_name"]
    file_path.write_bytes(b"tampered")
    with pytest.raises(ArtifactQualificationError, match="artifact_integrity_unverified"):
        _verify(tts_inputs, digest)
