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
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.voice_catalogue_http import validate_response

from speechnexus_backend.speech.application.voice_validation_report import (
    VALIDATION_MAX_FRAMES,
    VALIDATION_MAX_FRAMES_PER_CHAR,
    VALIDATION_MIN_FRAMES,
    VALIDATION_MIN_FRAMES_PER_CHAR,
    VALIDATION_MIN_PEAK,
    VALIDATION_MIN_RMS,
    VALIDATION_PROBE_LANGUAGE,
)
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
_PROBE_KEYS = frozenset(
    {
        "name",
        "text",
        "text_sha256",
        "generation_preview_text_sha256",
        "language",
        "sample_rate_hz",
        "audio_format",
        "audio_duration_ms",
        "sample_frames",
        "output_rms",
        "output_peak",
        "output_sha256",
        "output_bytes",
        "mode",
    }
)
_LANGUAGE = re.compile(r"^(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)$")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


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


def _probes(
    reported: dict[str, Any], checks: tuple[_Check, ...], capabilities: dict[str, Any]
) -> list[dict[str, Any]]:
    value = reported.get("probes")
    if type(value) is not list or not value:
        raise VoiceEvidenceNotEligibleError("coverage_probes_missing")
    if len(value) != 1:
        # This slice measures exactly one language and one probe; a longer list
        # would claim coverage the run did not produce.
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    entry = value[0]
    if type(entry) is not dict or set(entry) != _PROBE_KEYS:
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    name = entry["name"]
    text = entry["text"]
    language = entry["language"]
    sample_rate = entry["sample_rate_hz"]
    duration = entry["audio_duration_ms"]
    frames = entry["sample_frames"]
    rms = entry["output_rms"]
    peak = entry["output_peak"]
    output_bytes = entry["output_bytes"]
    if name != "new_text_reuse" or not any(check.name == name for check in checks):
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    if not isinstance(text, str) or not 1 <= len(text) <= 2000:
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    if entry["text_sha256"] != _text_digest(text):
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    if (
        not isinstance(entry["generation_preview_text_sha256"], str)
        or entry["generation_preview_text_sha256"] == entry["text_sha256"]
    ):
        # The whole point of the probe is that it is not the generation text.
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    for field in ("text_sha256", "generation_preview_text_sha256", "output_sha256"):
        if not isinstance(entry[field], str) or _DIGEST.fullmatch(entry[field]) is None:
            raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    if (
        not isinstance(language, str)
        or _LANGUAGE.fullmatch(language) is None
        or language != VALIDATION_PROBE_LANGUAGE
    ):
        # The fixed probe is English; no other language was measured.
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    # This lane measures the fixed validation rate; a report claiming another
    # rate is not evidence this run produced.
    if sample_rate != 24_000 or type(sample_rate) is not int:
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    if entry["audio_format"] != "wav" or entry["mode"] != "complete":
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    if (
        type(duration) is not int
        or duration < 1
        or type(frames) is not int
        or type(rms) is not int
        or type(peak) is not int
        or type(output_bytes) is not int
        or output_bytes < 1
    ):
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    min_frames = max(VALIDATION_MIN_FRAMES, len(text) * VALIDATION_MIN_FRAMES_PER_CHAR)
    max_frames = min(VALIDATION_MAX_FRAMES, len(text) * VALIDATION_MAX_FRAMES_PER_CHAR)
    if (
        not min_frames <= frames <= max_frames
        or duration != frames * 1000 // sample_rate
        or output_bytes < frames * 2
        or rms < VALIDATION_MIN_RMS
        or peak < VALIDATION_MIN_PEAK
        or peak > 32_768
        or rms > peak
    ):
        # Recompute the quality floor from the report's measured values; a
        # silent, truncated, or implausibly short/long clip is not evidence.
        raise VoiceEvidenceNotEligibleError("coverage_probes_invalid")
    if set(capabilities) != {"languages", "sample_rates_hz", "audio_formats", "modes"}:
        raise VoiceEvidenceNotEligibleError("coverage_capabilities_mismatch")
    if (
        capabilities.get("languages") != [language]
        or capabilities.get("sample_rates_hz") != [sample_rate]
        or capabilities.get("audio_formats") != ["wav"]
        or capabilities.get("modes") != ["complete"]
    ):
        # Capabilities must be the probe's measured facts, not a broader claim.
        raise VoiceEvidenceNotEligibleError("coverage_capabilities_mismatch")
    return [
        {
            "name": name,
            "text": text,
            "text_sha256": entry["text_sha256"],
            "generation_preview_text_sha256": entry["generation_preview_text_sha256"],
            "language": language,
            "sample_rate_hz": sample_rate,
            "audio_format": "wav",
            "audio_duration_ms": duration,
            "sample_frames": frames,
            "output_rms": rms,
            "output_peak": peak,
            "output_sha256": entry["output_sha256"],
            "output_bytes": output_bytes,
            "mode": "complete",
        }
    ]


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _coverage(
    report: dict[str, Any],
    validation: dict[str, Any],
    checks: tuple[_Check, ...],
    capabilities: dict[str, Any],
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
    probes = _probes(reported, checks, capabilities)
    return {
        "synthesis_path": REUSABLE_TTS_PATH,
        "validation_set_revision": validation["validation_set_revision"],
        "checks": [
            {"name": item.name, "required": item.required, "status": item.status} for item in checks
        ],
        "required_checks": sorted(item.name for item in checks if item.required),
        "reusable_tts": "passed",
        "probes": probes,
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
    coverage = _coverage(document, validation, checks, capabilities)
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
