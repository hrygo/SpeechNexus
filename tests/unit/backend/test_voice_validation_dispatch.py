"""The validation lane's internal probe/report contract and its ledger boundary."""

from __future__ import annotations

import json
from dataclasses import replace

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
)
from speechnexus_backend.speech.domain.voice_capability_evidence import VoiceMaterial
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
    duration_ms=500,
    bytes=24_000,
    sha256="sha256:" + "d" * 64,
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
        "language": "en",
    }
    values.update(changes)
    return ValidationRunFacts(**values)  # type: ignore[arg-type]


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
    result_json = build_validation_result(
        facts,
        job_id="job-validation-1",
        artifact_id="report-1",
        receipt_id="receipt-1",
        created_at_us=0,
    )
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
    result = json.loads(
        build_validation_result(
            facts,
            job_id="job-validation-1",
            artifact_id="report-1",
            receipt_id="receipt-1",
            created_at_us=0,
        )
    )
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
    result = json.loads(
        build_validation_result(
            _run_facts(),
            job_id="job-validation-1",
            artifact_id="report-1",
            receipt_id="receipt-1",
            created_at_us=0,
        )
    )
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
