"""Offline artifact verification for a future controlled qualification probe.

This validates local bytes against a caller's already-authoritative pin digest. It
never asserts that a live instance loaded them or grants voice qualification.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from speechnexus_backend.speech.domain.publications import publication_digest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPOSITORY_ROOT / "scripts/dev-env/verify-file-asr-artifacts.py"
_MAX_PIN_BYTES = 256 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"


class ArtifactQualificationError(ValueError):
    """Fixed failure only; never include model paths or file contents."""


@dataclass(frozen=True, slots=True)
class VerifiedLocalArtifacts:
    pin_digest: str
    model_id: str
    model_revision: str
    model_artifact_digest: str
    expected_image_digest: str
    artifact_count: int


def verify_local_tts_artifacts(
    *,
    pin_path: Path,
    model_root: Path,
    expected_pin_digest: str,
    repository_root: Path = _REPOSITORY_ROOT,
) -> VerifiedLocalArtifacts:
    """Reuse the existing offline verifier; an authoritative pin digest is mandatory.

    The second pin read must hash to the exact bytes verified by that tool. The
    returned image is a pinned expectation, not a runtime observation. Callers
    must independently establish image, generation, stop and release facts.
    """
    try:
        if _DIGEST.fullmatch(expected_pin_digest) is None:
            raise ValueError
        spec = importlib.util.spec_from_file_location("speechnexus_offline_artifact_gate", _SCRIPT)
        if spec is None or spec.loader is None:
            raise ValueError
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report: dict[str, Any] = module.verify(
            pin_path=pin_path, model_root=model_root, repository_root=repository_root
        )
        with pin_path.open("rb") as stream:
            raw = stream.read(_MAX_PIN_BYTES + 1)
        if not 1 <= len(raw) <= _MAX_PIN_BYTES:
            raise ValueError
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if digest != expected_pin_digest or digest != report["pin_digest"]:
            raise ValueError
        pin = json.loads(raw)
        model, engine, artifacts = pin["model"], report["engine"], report["artifact_set"]
        if (
            report["version"] != 2
            or report["scope"] != "local_artifact_integrity"
            or model["repository"] != _MODEL
            or report["model"]["revision"] != model["revision"]
            or engine["kind"] != "vllm_omni"
            or not isinstance(engine["image_digest"], str)
            or _DIGEST.fullmatch(engine["image_digest"]) is None
            or not isinstance(artifacts, list)
            or len(artifacts) != 11
        ):
            raise ValueError
        return VerifiedLocalArtifacts(
            pin_digest=digest,
            model_id=model["repository"],
            model_revision=model["revision"],
            model_artifact_digest=publication_digest(model),
            expected_image_digest=engine["image_digest"],
            artifact_count=len(artifacts),
        )
    except Exception:
        raise ArtifactQualificationError("artifact_integrity_unverified") from None
