"""Immutable measured-capability evidence for one successful validation run.

The ledger is append-only. A fact that later turns out to be wrong is corrected by
running a new validation Job and recording a new evidence row; nothing in this
module describes an edit path.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import rfc8785

EVIDENCE_KIND = "validation_run"
REUSABLE_TTS_PATH = "reusable_tts"
MAX_TIMESTAMP_US = 253_402_300_799_999_999
MAX_MEASURED_BY = 256

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_REVISION = re.compile(r"[1-9][0-9]{0,18}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def require_identifier(value: object, name: str = "identifier") -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"Invalid {name}")
    return value


def require_revision(value: object, name: str = "revision") -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise ValueError(f"Invalid {name}")
    return value


def require_digest(value: object, name: str = "digest") -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"Invalid {name}")
    return value


def evidence_id_for_job(validation_job_id: str) -> str:
    return "vce-" + require_identifier(validation_job_id, "validation job id")


def voice_id_for_draft(draft_id: str) -> str:
    """Stable voice identity; a draft never publishes under a second voice."""
    require_identifier(draft_id, "draft id")
    return "voice-" + draft_id.removeprefix("draft-")


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return prefix + digest[:40]


def publication_id_for(voice_id: str, revision: str) -> str:
    return _stable_id("vpub-", voice_id, revision)


def revision_id_for(voice_id: str, revision: str) -> str:
    return _stable_id("vrev-", voice_id, revision)


def asset_reference_id(voice_id: str, revision: str, asset_id: str) -> str:
    return _stable_id("ref-", voice_id, revision, asset_id)


@dataclass(frozen=True, slots=True)
class VoiceMaterial:
    """The exact voice reference an immutable revision is bound to."""

    asset_id: str
    sha256: str
    bytes: int
    media_type: str

    def __post_init__(self) -> None:
        require_identifier(self.asset_id, "material asset id")
        require_digest(self.sha256, "material digest")
        if type(self.bytes) is not int or self.bytes <= 0:
            raise ValueError("Invalid material byte length")
        if not isinstance(self.media_type, str) or not 1 <= len(self.media_type) <= 128:
            raise ValueError("Invalid material media type")

    @classmethod
    def from_value(cls, value: object) -> VoiceMaterial:
        if type(value) is not dict or set(value) != {"asset_id", "sha256", "bytes", "media_type"}:
            raise ValueError("Invalid material document")
        return cls(
            asset_id=value["asset_id"],
            sha256=value["sha256"],
            bytes=value["bytes"],
            media_type=value["media_type"],
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "media_type": self.media_type,
        }


def _canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class VoiceCapabilityEvidence:
    """One measured capability run, keyed by the Job that produced it."""

    evidence_id: str
    draft_id: str
    draft_revision: str
    validation_job_id: str
    validation_id: str
    binding_revision_id: str
    report_artifact_id: str
    report_sha256: str
    report_receipt_id: str
    evidence_kind: str
    measured_by: str
    measured_at_us: int
    capabilities: dict[str, Any]
    coverage: dict[str, Any]
    material: VoiceMaterial | None
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_id != evidence_id_for_job(self.validation_job_id):
            raise ValueError("Evidence identity does not derive from its validation Job")
        require_identifier(self.draft_id, "draft id")
        require_revision(self.draft_revision, "draft revision")
        require_identifier(self.validation_id, "validation id")
        require_identifier(self.binding_revision_id, "binding revision id")
        require_identifier(self.report_artifact_id, "report artifact id")
        require_digest(self.report_sha256, "report digest")
        require_identifier(self.report_receipt_id, "report receipt id")
        if self.evidence_kind != EVIDENCE_KIND:
            raise ValueError("Unknown evidence kind")
        if (
            not isinstance(self.measured_by, str)
            or not 1 <= len(self.measured_by) <= MAX_MEASURED_BY
            or any(character in self.measured_by for character in "\r\n\t")
        ):
            raise ValueError("Invalid evidence measurer")
        if type(self.measured_at_us) is not int or not 0 <= self.measured_at_us <= MAX_TIMESTAMP_US:
            raise ValueError("Invalid evidence timestamp")
        if type(self.capabilities) is not dict or not self.capabilities:
            raise ValueError("Invalid measured capabilities")
        if type(self.coverage) is not dict or not self.coverage:
            raise ValueError("Invalid measurement coverage")
        if self.material is not None and not isinstance(self.material, VoiceMaterial):
            raise ValueError("Invalid measured material")
        expected = self.compute_digest(
            draft_id=self.draft_id,
            draft_revision=self.draft_revision,
            validation_job_id=self.validation_job_id,
            validation_id=self.validation_id,
            binding_revision_id=self.binding_revision_id,
            report_artifact_id=self.report_artifact_id,
            report_sha256=self.report_sha256,
            report_receipt_id=self.report_receipt_id,
            evidence_kind=self.evidence_kind,
            measured_by=self.measured_by,
            capabilities=self.capabilities,
            coverage=self.coverage,
            material=None if self.material is None else self.material.as_json(),
        )
        if self.evidence_digest != expected:
            raise ValueError("Evidence digest does not match its facts")

    @staticmethod
    def compute_digest(
        *,
        draft_id: str,
        draft_revision: str,
        validation_job_id: str,
        validation_id: str,
        binding_revision_id: str,
        report_artifact_id: str,
        report_sha256: str,
        report_receipt_id: str,
        evidence_kind: str,
        measured_by: str,
        capabilities: dict[str, Any],
        coverage: dict[str, Any],
        material: dict[str, Any] | None,
    ) -> str:
        return _canonical_digest(
            {
                "draft_id": draft_id,
                "draft_revision": draft_revision,
                "validation_job_id": validation_job_id,
                "validation_id": validation_id,
                "binding_revision_id": binding_revision_id,
                "report_artifact_id": report_artifact_id,
                "report_sha256": report_sha256,
                "report_receipt_id": report_receipt_id,
                "evidence_kind": evidence_kind,
                "measured_by": measured_by,
                # Observation time is not evidence identity: an identical replay
                # of the same measurement must not become a conflict.
                "capabilities": capabilities,
                "coverage": coverage,
                "material": material,
            }
        )

    @property
    def synthesis_path(self) -> str:
        value = self.coverage.get("synthesis_path")
        return value if isinstance(value, str) else ""

    def as_row(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "draft_id": self.draft_id,
            "draft_revision": self.draft_revision,
            "validation_job_id": self.validation_job_id,
            "validation_id": self.validation_id,
            "binding_revision_id": self.binding_revision_id,
            "report_artifact_id": self.report_artifact_id,
            "report_sha256": self.report_sha256,
            "report_receipt_id": self.report_receipt_id,
            "evidence_kind": self.evidence_kind,
            "measured_by": self.measured_by,
            "measured_at_us": self.measured_at_us,
            "capabilities": self.capabilities,
            "coverage": self.coverage,
            "material": None if self.material is None else self.material.as_json(),
            "evidence_digest": self.evidence_digest,
        }
