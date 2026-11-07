"""The validation lane's internal probe/report contract and its ledger boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.voice_capability_evidence import (
    VoiceEvidenceNotEligibleError,
    build_evidence,
    committed_validation,
)
from speechnexus_backend.speech.application.voice_validation_report import (
    ValidationRunFacts,
    WaveFacts,
    build_validation_report,
    build_validation_result,
    validate_voice_validation_result,
    validation_outcome,
)
from speechnexus_backend.speech.domain.job_dispatch import VOICE_VALIDATION_JOB_KINDS
from speechnexus_backend.speech.domain.voice_capability_evidence import VoiceMaterial
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
)
from speechnexus_backend.speech.infrastructure.voice_validation_submit import (
    validation_probe_text,
)
from tests.unit.backend.test_voice_capability_evidence import _facts

MATERIAL = VoiceMaterial("asset-reference", "sha256:" + "b" * 64, 64, "audio/wav")
GENERATION_PREVIEW = "Generation-stage preview sentence."
PROBE_TEXT = validation_probe_text("job-validation-1")
MEASURED = WaveFacts(
    sample_rate_hz=24_000,
    channels=1,
    duration_ms=2_000,
    bytes=96_044,
    sha256="sha256:" + "d" * 64,
    frames=48_000,
    rms=2_000,
    peak=12_000,
)
SILENT_MEASURED = WaveFacts(
    sample_rate_hz=24_000,
    channels=1,
    duration_ms=1,
    bytes=46,
    sha256="sha256:" + "e" * 64,
    frames=24,
    rms=0,
    peak=0,
)


def _run_facts(**changes: object) -> ValidationRunFacts:
    values: dict[str, object] = {
        "draft_id": "draft-reference",
        "draft_revision": "1",
        "validation_set_revision": "1",
        "binding_id": "voice-model",
        "binding_revision": "1",
        "generation_preview_text": GENERATION_PREVIEW,
        "material": MATERIAL,
        "reference_text": None,
    }
    values.update(changes)
    return ValidationRunFacts(**values)  # type: ignore[arg-type]


def _result_json(
    facts: ValidationRunFacts | None = None,
    *,
    measured: WaveFacts = MEASURED,
    artifact_id: str = "report-1",
) -> str:
    facts = facts or _run_facts()
    report = build_validation_report(facts, probe_text=PROBE_TEXT, measured=measured)
    checks, eligible = validation_outcome(report)
    return build_validation_result(
        facts,
        job_id="job-validation-1",
        artifact_id=artifact_id,
        receipt_id="receipt-1",
        created_at_us=0,
        checks=checks,
        publication_eligible=eligible,
    )


def test_probe_text_is_stable_and_distinct_from_the_generation_preview() -> None:
    assert validation_probe_text("job-validation-1") == validation_probe_text("job-validation-1")
    assert validation_probe_text("job-validation-1") != validation_probe_text("job-validation-2")
    assert PROBE_TEXT != GENERATION_PREVIEW


def test_report_rejects_reusing_the_generation_preview_as_the_probe() -> None:
    facts = _run_facts(generation_preview_text=PROBE_TEXT)
    with pytest.raises(ValueError, match="reuses the generation preview"):
        build_validation_report(facts, probe_text=PROBE_TEXT, measured=MEASURED)


def test_result_is_closed_and_references_the_report() -> None:
    facts = _run_facts()
    result_json = _result_json(facts)
    result = json.loads(result_json)
    assert set(result) == {
        "kind",
        "validation",
        "validation_report_artifact_id",
        "receipt_id",
    }
    assert result["validation_report_artifact_id"] == "report-1"
    assert set(result["validation"]["checks"][0]) == {"name", "required", "status"}
    validate_voice_validation_result(result)


def test_result_without_the_report_artifact_id_is_rejected() -> None:
    facts = _run_facts()
    result = json.loads(_result_json(facts))
    del result["validation_report_artifact_id"]
    with pytest.raises(ValueError, match="outside the frozen contract"):
        validate_voice_validation_result(result)
    evidence_facts = replace(
        _facts(b'{"placeholder":true}'),
        result_json=json.dumps(result),
    )
    with pytest.raises(VoiceEvidenceNotEligibleError, match="validation_report_mismatch"):
        committed_validation(evidence_facts)


def test_public_check_detail_is_rejected_by_the_evidence_ledger() -> None:
    report = build_validation_report(_run_facts(), probe_text=PROBE_TEXT, measured=MEASURED)
    result = json.loads(_result_json())
    for check in result["validation"]["checks"]:
        check["detail"] = "a public optional field the ledger does not store"
    evidence_facts = _facts(report)
    # The public result schema accepts ``detail``; the ledger must not.
    validate_voice_validation_result(result)
    with pytest.raises(VoiceEvidenceNotEligibleError, match="validation_checks_invalid"):
        committed_validation(replace(evidence_facts, result_json=json.dumps(result)))


def test_report_probe_survives_into_evidence_coverage() -> None:
    report = build_validation_report(_run_facts(), probe_text=PROBE_TEXT, measured=MEASURED)
    evidence = build_evidence(_facts(report), report, measured_by="worker", measured_at_us=1)
    probe = evidence.coverage["probes"][0]
    assert probe["text"] == PROBE_TEXT
    assert probe["generation_preview_text_sha256"] != probe["text_sha256"]
    assert evidence.capabilities == {
        "languages": ["en"],
        "sample_rates_hz": [24_000],
        "audio_formats": ["wav"],
        "modes": ["complete"],
    }


def test_evidence_rejects_a_capability_claim_the_run_did_not_measure() -> None:
    report = build_validation_report(_run_facts(), probe_text=PROBE_TEXT, measured=MEASURED)
    document = json.loads(report)
    document["capabilities"]["sample_rates_hz"] = [48_000]
    document["coverage"]["probes"][0]["sample_rate_hz"] = 48_000
    changed = json.dumps(document, separators=(",", ":")).encode()
    with pytest.raises(VoiceEvidenceNotEligibleError, match="coverage_probes_invalid"):
        build_evidence(_facts(changed), changed, measured_by="worker", measured_at_us=1)


def test_one_millisecond_of_digital_silence_is_failed_and_not_publishable() -> None:
    facts = _run_facts()
    report = build_validation_report(facts, probe_text=PROBE_TEXT, measured=SILENT_MEASURED)
    document = json.loads(report)
    assert {check["status"] for check in document["coverage"]["checks"]} == {"failed"}
    _checks, eligible = validation_outcome(report)
    assert eligible is False
    result = json.loads(_result_json(facts, measured=SILENT_MEASURED))
    assert result["validation"]["status"] == "failed"
    assert result["validation"]["publication_eligible"] is False
    assert {check["status"] for check in result["validation"]["checks"]} == {"failed"}
    validate_voice_validation_result(result)
    # The ledger refuses the same failed report even if the caller tries.
    with pytest.raises(VoiceEvidenceNotEligibleError):
        build_evidence(_facts(report), report, measured_by="worker", measured_at_us=1)

def test_validation_queue_rejects_the_legacy_anchor_only_lane() -> None:
    with pytest.raises(ValueError, match="requires admission"):
        PostgresDispatchQueue(
            cast(Any, object()),
            kinds=VOICE_VALIDATION_JOB_KINDS,
            require_admission=False,
        )
