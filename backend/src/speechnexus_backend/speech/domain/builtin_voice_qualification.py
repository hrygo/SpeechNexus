"""The fixed facts an independent qualification producer must attest."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIGITS = re.compile(r"[1-9][0-9]{0,18}\Z")
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
_PROBE_CHECKS = frozenset(
    {
        "source_approved",
        "artifact_integrity",
        "engine_identity",
        "voice_speaker_binding",
        "wav_complete",
        "output_bounds",
        "native_terminal",
        "stop_release",
    }
)
_PROBE_VERIFIER = ("builtin-customvoice-v1", "1")

# The operator-attested variant replaces the probe evidence digest with the
# digest of the declaration bundle and carries the attestation that produced it.
# Its key set is disjoint from the probe key set, so a record can never be read
# as the other source: only one of the two producers can have written it.
_OPERATOR_FIELDS = (_FIELDS - {"probe_evidence_digest"}) | {"declaration_digest", "attestation"}
_OPERATOR_DIGEST_FIELDS = (_DIGEST_FIELDS - {"probe_evidence_digest"}) | {"declaration_digest"}
_OPERATOR_CHECKS = frozenset(
    {
        "source_approved",
        "artifact_integrity",
        "engine_identity",
        "voice_speaker_binding",
        "declared_capabilities",
        "instance_registration",
        "attestation_window",
        "gateway_policy",
    }
)
_OPERATOR_VERIFIER = ("operator-attested-customvoice-v1", "1")
_OPERATOR_ATTESTATION_FIELDS = frozenset(
    {
        "kind",
        "attestation_id",
        "approval_id",
        "declared_by",
        "declaration_digest",
        "template_digest",
        "lifecycle_digest",
        "image_digest",
        "model_artifact_digest",
        "instance_id",
        "instance_generation",
        "fencing_token",
        "observed_at_us",
        "expires_at_us",
    }
)


@dataclass(frozen=True, slots=True)
class BuiltinVoiceFacts:
    """Validation is structural; only a separate trusted producer may attest facts."""

    value: dict[str, Any]

    def __post_init__(self) -> None:
        v = self.value
        if type(v) is not dict:
            raise ValueError("incomplete qualification facts")
        if set(v) == _OPERATOR_FIELDS:
            operator = True
            digest_fields, free_fields = _OPERATOR_DIGEST_FIELDS, _OPERATOR_FIELDS
        elif set(v) == _FIELDS:
            operator = False
            digest_fields, free_fields = _DIGEST_FIELDS, _FIELDS
        else:
            raise ValueError("incomplete qualification facts")
        for name in free_fields - {
            "required_checks",
            "public_document",
            "qualified_capabilities",
            "checked_at_us",
            "expires_at_us",
            "attestation",
        }:
            if not isinstance(v[name], str) or not v[name] or len(v[name]) > 512:
                raise ValueError("invalid qualification fact")
        if any(_DIGEST.fullmatch(v[name]) is None for name in digest_fields):
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
        verifier = (v["verifier_id"], v["verifier_version"])
        expected_verifier = _OPERATOR_VERIFIER if operator else _PROBE_VERIFIER
        required = _OPERATOR_CHECKS if operator else _PROBE_CHECKS
        if verifier != expected_verifier:
            raise ValueError("unsupported verifier")
        if not isinstance(checks, list) or len(checks) != len(required) or set(checks) != required:
            raise ValueError("invalid required checks")
        if operator:
            self._require_attestation(v)
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

    @staticmethod
    def _require_attestation(v: dict[str, Any]) -> None:
        """The operator variant must describe itself consistently, not by label only."""
        attestation = v["attestation"]
        if type(attestation) is not dict or set(attestation) != _OPERATOR_ATTESTATION_FIELDS:
            raise ValueError("invalid attestation facts")
        if attestation["kind"] != "operator_attested":
            raise ValueError("invalid attestation facts")
        if not isinstance(attestation["attestation_id"], str) or not attestation["attestation_id"]:
            raise ValueError("invalid attestation facts")
        if (
            attestation["declaration_digest"] != v["declaration_digest"]
            or attestation["image_digest"] != v["image_digest"]
            or attestation["model_artifact_digest"] != v["model_artifact_digest"]
            or attestation["instance_id"] != v["instance_id"]
            or attestation["instance_generation"] != v["generation"]
            or attestation["observed_at_us"] != v["checked_at_us"]
            or attestation["expires_at_us"] != v["expires_at_us"]
        ):
            raise ValueError("attestation facts disagree with qualification")
        if any(
            _DIGEST.fullmatch(attestation[name]) is None
            for name in ("declaration_digest", "template_digest", "lifecycle_digest")
        ):
            raise ValueError("invalid attestation digest")
        if (
            type(attestation["observed_at_us"]) is not int
            or type(attestation["expires_at_us"]) is not int
        ):
            raise ValueError("invalid attestation window")
        if not isinstance(attestation["declared_by"], str) or not attestation["declared_by"]:
            raise ValueError("invalid attestation facts")
        if not isinstance(attestation["fencing_token"], str) or not _DIGITS.fullmatch(
            attestation["fencing_token"]
        ):
            raise ValueError("invalid attestation facts")
        if not isinstance(attestation["approval_id"], str) or not attestation["approval_id"]:
            raise ValueError("invalid attestation facts")
