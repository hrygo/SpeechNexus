"""The fixed facts an independent qualification producer must attest."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FIELDS = frozenset(
    {
        "source_approval_id",
        "source_revision",
        "source_digest",
        "qualification_report_id",
        "qualification_report_digest",
        "capacity_manifest_id",
        "capacity_manifest_digest",
        "probe_evidence_digest",
        "verifier_id",
        "verifier_version",
        "required_checks",
        "model_id",
        "model_revision",
        "model_artifact_digest",
        "image_digest",
        "generation",
        "instance_id",
        "host_node_id",
        "gateway_id",
        "gateway_policy_revision_id",
        "gateway_policy_digest",
        "binding_id",
        "binding_revision",
        "binding_revision_id",
        "native_speaker_id",
        "checked_at_us",
        "expires_at_us",
        "public_document",
        "qualified_capabilities",
    }
)
_DIGEST_FIELDS = frozenset(
    {
        "source_digest",
        "qualification_report_digest",
        "capacity_manifest_digest",
        "probe_evidence_digest",
        "model_artifact_digest",
        "image_digest",
        "gateway_policy_digest",
    }
)


@dataclass(frozen=True, slots=True)
class BuiltinVoiceFacts:
    """Validation is structural; only a separate trusted producer may attest facts."""

    value: dict[str, Any]

    def __post_init__(self) -> None:
        v = self.value
        if type(v) is not dict or set(v) != _FIELDS:
            raise ValueError("incomplete qualification facts")
        for name in _FIELDS - {
            "required_checks",
            "public_document",
            "qualified_capabilities",
            "checked_at_us",
            "expires_at_us",
        }:
            if not isinstance(v[name], str) or not v[name] or len(v[name]) > 512:
                raise ValueError("invalid qualification fact")
        if any(_DIGEST.fullmatch(v[name]) is None for name in _DIGEST_FIELDS):
            raise ValueError("invalid evidence digest")
        if (
            v["native_speaker_id"] != "Vivian"
            or v["model_id"] != "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
        ):
            raise ValueError("unsupported builtin voice")
        if not re.fullmatch(r"[1-9][0-9]{0,18}", v["binding_revision"]):
            raise ValueError("invalid binding revision")
        if (
            any(type(v[x]) is not int or v[x] <= 0 for x in ("checked_at_us", "expires_at_us"))
            or v["expires_at_us"] <= v["checked_at_us"]
        ):
            raise ValueError("invalid qualification validity")
        checks = v["required_checks"]
        required = {
            "source_approved",
            "artifact_integrity",
            "engine_identity",
            "voice_speaker_binding",
            "wav_complete",
            "output_bounds",
            "native_terminal",
            "stop_release",
        }
        if (v["verifier_id"], v["verifier_version"]) != ("builtin-customvoice-v1", "1"):
            raise ValueError("unsupported verifier")
        if not isinstance(checks, list) or len(checks) != len(required) or set(checks) != required:
            raise ValueError("invalid required checks")
        if type(v["qualified_capabilities"]) is not dict:
            raise ValueError("invalid qualified capabilities")
        document = v["public_document"]
        if type(document) is not dict:
            raise ValueError("invalid voice document")
        if document.get("source") != {
            "type": "builtin",
            "draft_id": None,
            "draft_revision": None,
            "source_asset_digest": None,
        }:
            raise ValueError("invalid builtin source")
        if document.get("status") != "published" or document.get("model_binding") != {
            "id": v["binding_id"],
            "revision": v["binding_revision"],
        }:
            raise ValueError("voice binding mismatch")
        validation = document.get("validation")
        if (
            not isinstance(validation, dict)
            or validation.get("validation_id") != v["qualification_report_id"]
        ):
            raise ValueError("voice validation mismatch")
        checked_at = datetime.fromisoformat(validation["checked_at"].replace("Z", "+00:00"))
        if checked_at.tzinfo is None:
            raise ValueError("voice validation time missing timezone")
        delta = checked_at.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        if (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds != v[
            "checked_at_us"
        ]:
            raise ValueError("voice validation time mismatch")
