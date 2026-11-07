"""Record measured voice capabilities from a successful validation run.

This is the only path that creates capability evidence. It refuses to infer a
capability: every value comes from the committed validation report produced by
the validation consumer, and the report's digest is the digest of the bytes the
operator handed to the recorder. The publication transaction later re-reads the
evidence row but never opens object storage.

The report document consumed here is private to the execution path:

``{"synthesis_path": "reusable_tts", "capabilities": {...}, "coverage": {...},
  "material": {...} | null}``

``capabilities`` must satisfy the public ``VoiceCapabilities`` shape, ``coverage``
must repeat the Job's validation set revision and checks, and ``material`` must
name the exact ready asset and bytes the draft revision already pinned. A report
that omits these facts is rejected; nothing is derived from an audio file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.voice_catalogue_http import validate_response

from speechnexus_backend.speech.domain.voice_capability_evidence import (
    EVIDENCE_KIND,
    REUSABLE_TTS_PATH,
    VoiceCapabilityEvidence,
    VoiceMaterial,
    evidence_id_for_job,
    require_digest,
    require_identifier,
)

_MAX_REPORT_BYTES = 1_048_576
_CHECK_STATUSES = frozenset({"passed", "failed", "skipped"})


class VoiceEvidenceNotEligibleError(ValueError):
    """The run cannot support publication of the target draft revision."""


class VoiceEvidenceConflictError(RuntimeError):
    """A different evidence row already exists for the same validation Job."""


class VoiceReportUnavailableError(RuntimeError):
    """The committed report bytes or artifact facts are unavailable."""


class VoiceEvidenceUnavailableError(RuntimeError):
    """Evidence storage could not answer; nothing may be inferred from it."""


@dataclass(frozen=True, slots=True)
class ValidationRunFacts:
    """Committed facts read under one transaction before evidence is written."""

    validation_job_id: str
    job_kind: str
    job_state: str
    result_json: str | None
    draft_id: str
    draft_revision: str
    draft_kind: str
    draft_input: dict[str, Any]
    binding: dict[str, str]
    binding_revision_id: str
    report_artifact_id: str
    report_receipt_id: str
    report_sha256: str
    report_bytes: int
    source_asset: VoiceMaterial | None


class VoiceEvidenceStore(Protocol):
    def record(
        self, validation_job_id: str, report: bytes, measured_by: str
    ) -> tuple[VoiceCapabilityEvidence, bool]: ...


@dataclass(frozen=True, slots=True)
class _Check:
    name: str
    required: bool
    status: str


def _unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def parse_validation_report(raw: bytes) -> dict[str, Any]:
    if not 1 <= len(raw) <= _MAX_REPORT_BYTES:
        raise VoiceReportUnavailableError
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_nonfinite
        )
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise VoiceReportUnavailableError from None
    if type(document) is not dict:
        raise VoiceReportUnavailableError
    return document


@lru_cache(maxsize=1)
def _revision_template() -> dict[str, Any]:
    return {
        "resource_type": "voice_revision",
        "voice_id": "voice-evidence",
        "revision": "1",
        "object": "nexus.voice_revision",
        "status": "published",
        "display_name": "measured capability",
        "source": {
            "type": "cloned",
            "draft_id": "draft-evidence",
            "draft_revision": "1",
            "source_asset_digest": "sha256:" + "0" * 64,
        },
        "model_binding": {"id": "binding-evidence", "revision": "1"},
        "capabilities": {},
        "validation": {
            "validation_id": "validation-evidence",
            "status": "passed",
            "checked_at": "2026-01-01T00:00:00Z",
        },
        "published_at": "2026-01-01T00:00:00Z",
        "retired_at": None,
        "disabled_reason": None,
    }


def validate_capabilities(capabilities: object) -> dict[str, Any]:
    """Validate the measured capabilities against the public revision shape.

    ``getVoiceRevision`` is the frozen consumer of ``VoiceCapabilities``, so a
    synthetic revision using the measured object is validated by the same
    generated contract without changing that contract.
    """
    if type(capabilities) is not dict:
        raise VoiceEvidenceNotEligibleError("capabilities_missing")
    document = {**_revision_template(), "capabilities": capabilities}
    try:
        validate_response("getVoiceRevision", 200, {"body": document})
    except ContractValidationError:
        raise VoiceEvidenceNotEligibleError("capabilities_invalid") from None
    return capabilities


def _checks(value: object) -> tuple[_Check, ...]:
    if type(value) is not list or not value:
        raise VoiceEvidenceNotEligibleError("validation_checks_missing")
    checks: list[_Check] = []
    for entry in value:
        if type(entry) is not dict or set(entry) != {"name", "required", "status"}:
            raise VoiceEvidenceNotEligibleError("validation_checks_invalid")
        name, required, status = entry["name"], entry["required"], entry["status"]
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 128
            or type(required) is not bool
            or status not in _CHECK_STATUSES
        ):
            raise VoiceEvidenceNotEligibleError("validation_checks_invalid")
        checks.append(_Check(name=name, required=required, status=status))
    return tuple(checks)


def committed_validation(facts: ValidationRunFacts) -> tuple[dict[str, Any], tuple[_Check, ...]]:
    if facts.job_kind != "voice_validation" or facts.job_state != "succeeded":
        raise VoiceEvidenceNotEligibleError("validation_job_not_succeeded")
    if facts.result_json is None:
        raise VoiceEvidenceNotEligibleError("validation_result_missing")
    try:
        result = json.loads(
            facts.result_json, object_pairs_hook=_unique_pairs, parse_constant=_nonfinite
        )
    except ValueError, TypeError, RecursionError:
        raise VoiceEvidenceNotEligibleError("validation_result_invalid") from None
    if type(result) is not dict or result.get("kind") != "voice_validation":
        raise VoiceEvidenceNotEligibleError("validation_result_invalid")
    if (
        result.get("validation_report_artifact_id") != facts.report_artifact_id
        or result.get("receipt_id") != facts.report_receipt_id
    ):
        # The report this evidence would digest is not the report the Job
        # committed; recording it would qualify a different run.
        raise VoiceEvidenceNotEligibleError("validation_report_mismatch")
    validation = result.get("validation")
    checks = _checks(validation.get("checks") if type(validation) is dict else None)
    if type(validation) is not dict:
        raise VoiceEvidenceNotEligibleError("validation_result_invalid")
    if validation.get("model_binding") != facts.binding:
        raise VoiceEvidenceNotEligibleError("validation_binding_mismatch")
    if validation.get("status") != "passed" or validation.get("publication_eligible") is not True:
        raise VoiceEvidenceNotEligibleError("validation_not_eligible")
    if not any(check.name == "reusable_tts" and check.required for check in checks):
        raise VoiceEvidenceNotEligibleError("reusable_tts_not_required")
    if any(check.required and check.status != "passed" for check in checks):
        raise VoiceEvidenceNotEligibleError("validation_required_check_failed")
    reusable = next(check for check in checks if check.name == "reusable_tts")
    if reusable.status != "passed":
        raise VoiceEvidenceNotEligibleError("reusable_tts_not_passed")
    return validation, checks


def _coverage(
    report: dict[str, Any], validation: dict[str, Any], checks: tuple[_Check, ...]
) -> dict[str, Any]:
    reported = report.get("coverage")
    if type(reported) is not dict:
        raise VoiceEvidenceNotEligibleError("coverage_missing")
    if reported.get("validation_set_revision") != validation.get("validation_set_revision"):
        raise VoiceEvidenceNotEligibleError("coverage_revision_mismatch")
    reported_checks = _checks(reported.get("checks"))
    if sorted((item.name, item.required, item.status) for item in reported_checks) != sorted(
        (item.name, item.required, item.status) for item in checks
    ):
        # The report may not claim coverage the Job did not commit.
        raise VoiceEvidenceNotEligibleError("coverage_checks_mismatch")
    return {
        "synthesis_path": REUSABLE_TTS_PATH,
        "validation_set_revision": validation["validation_set_revision"],
        "checks": [
            {"name": item.name, "required": item.required, "status": item.status} for item in checks
        ],
        "required_checks": sorted(item.name for item in checks if item.required),
        "reusable_tts": "passed",
    }


def _material(report: dict[str, Any], facts: ValidationRunFacts) -> VoiceMaterial | None:
    reported = report.get("material")
    if facts.draft_kind != "clone":
        if reported is not None:
            raise VoiceEvidenceNotEligibleError("design_material_not_allowed")
        return None
    if facts.source_asset is None:
        raise VoiceEvidenceNotEligibleError("clone_material_missing")
    if type(reported) is not dict:
        raise VoiceEvidenceNotEligibleError("clone_material_missing")
    try:
        material = VoiceMaterial.from_value(reported)
    except ValueError:
        raise VoiceEvidenceNotEligibleError("clone_material_invalid") from None
    if material != facts.source_asset:
        raise VoiceEvidenceNotEligibleError("clone_material_mismatch")
    return material


def build_evidence(
    facts: ValidationRunFacts,
    report: bytes,
    *,
    measured_by: str,
    measured_at_us: int,
) -> VoiceCapabilityEvidence:
    """Build one evidence fact set or refuse with a stable eligibility reason."""
    if (
        not isinstance(measured_by, str)
        or not 1 <= len(measured_by) <= 256
        or any(character in measured_by for character in "\r\n\t")
    ):
        raise VoiceEvidenceNotEligibleError("invalid_measured_by")
    if hashlib.sha256(report).hexdigest() != facts.report_sha256.removeprefix("sha256:"):
        # The bytes the operator supplied are not the bytes the Artifact commits.
        raise VoiceEvidenceNotEligibleError("report_bytes_mismatch")
    if len(report) != facts.report_bytes:
        raise VoiceEvidenceNotEligibleError("report_bytes_mismatch")
    document = parse_validation_report(report)
    if document.get("synthesis_path") != REUSABLE_TTS_PATH:
        raise VoiceEvidenceNotEligibleError("synthesis_path_not_reusable")
    capabilities = validate_capabilities(document.get("capabilities"))
    validation, checks = committed_validation(facts)
    # The measured run must name the exact draft revision it qualifies. The
    # publication path re-checks this against its own target; keeping the check
    # here rejects a result whose target was rewritten before recording.
    if validation.get("draft_id") != facts.draft_id:
        raise VoiceEvidenceNotEligibleError("validation_draft_mismatch")
    if validation.get("draft_revision") != facts.draft_revision:
        raise VoiceEvidenceNotEligibleError("validation_revision_mismatch")
    coverage = _coverage(document, validation, checks)
    material = _material(document, facts)
    validation_id = require_identifier(validation.get("id"), "validation id")
    evidence_id = evidence_id_for_job(facts.validation_job_id)
    digest = VoiceCapabilityEvidence.compute_digest(
        draft_id=facts.draft_id,
        draft_revision=facts.draft_revision,
        validation_job_id=facts.validation_job_id,
        validation_id=validation_id,
        binding_revision_id=facts.binding_revision_id,
        report_artifact_id=facts.report_artifact_id,
        report_sha256=facts.report_sha256,
        report_receipt_id=facts.report_receipt_id,
        evidence_kind=EVIDENCE_KIND,
        measured_by=measured_by,
        capabilities=capabilities,
        coverage=coverage,
        material=None if material is None else material.as_json(),
    )
    return VoiceCapabilityEvidence(
        evidence_id=evidence_id,
        draft_id=facts.draft_id,
        draft_revision=facts.draft_revision,
        validation_job_id=facts.validation_job_id,
        validation_id=validation_id,
        binding_revision_id=facts.binding_revision_id,
        report_artifact_id=facts.report_artifact_id,
        report_sha256=require_digest(facts.report_sha256, "report digest"),
        report_receipt_id=facts.report_receipt_id,
        evidence_kind=EVIDENCE_KIND,
        measured_by=measured_by,
        measured_at_us=measured_at_us,
        capabilities=capabilities,
        coverage=coverage,
        material=material,
        evidence_digest=digest,
    )
