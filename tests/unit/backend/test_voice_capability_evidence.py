"""Pure evidence/publication facts: stable identities, rejection reasons, projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.voice_capability_evidence import (
    ValidationRunFacts,
    VoiceEvidenceNotEligibleError,
    VoiceReportUnavailableError,
    build_evidence,
    committed_validation,
    parse_validation_report,
    validate_capabilities,
)
from speechnexus_backend.speech.application.voice_publications import (
    PublicationRequest,
    VoicePublicationContractError,
    VoicePublicationInputError,
    VoicePublicationNotEligibleError,
    VoicePublications,
    build_evidence_checked_at_us,
    build_publication_view,
    build_voice_revision_document,
    require_revision_text,
    require_source_eligibility,
    stable_revision_id,
    stable_voice_id,
    timestamp,
)
from speechnexus_backend.speech.domain.voice_capability_evidence import (
    VoiceMaterial,
    asset_reference_id,
    evidence_id_for_job,
    publication_id_for,
    require_digest,
    require_identifier,
    require_revision,
    revision_id_for,
    voice_id_for_draft,
)

REPORT_SHA = "sha256:" + "a" * 64
CAPABILITIES = {
    "languages": ["en"],
    "sample_rates_hz": [24000],
    "audio_formats": ["wav"],
    "modes": ["complete"],
}
MATERIAL = {
    "asset_id": "asset-reference",
    "sha256": "sha256:" + "b" * 64,
    "bytes": 64,
    "media_type": "audio/wav",
}
CHECKS = [
    {"name": "new_text_reuse", "required": True, "status": "passed"},
    {"name": "reusable_tts", "required": True, "status": "passed"},
]
PROBE_TEXT = "A validation-only sentence."
PROBE = {
    "name": "new_text_reuse",
    "text": PROBE_TEXT,
    "text_sha256": "sha256:" + hashlib.sha256(PROBE_TEXT.encode()).hexdigest(),
    "generation_preview_text_sha256": "sha256:" + "c" * 64,
    "language": "en",
    "sample_rate_hz": 24000,
    "audio_format": "wav",
    "audio_duration_ms": 500,
    "output_sha256": "sha256:" + "d" * 64,
    "output_bytes": 24000,
    "mode": "complete",
}


def _report(**changes: Any) -> bytes:
    document = {
        "synthesis_path": "reusable_tts",
        "capabilities": CAPABILITIES,
        "coverage": {"validation_set_revision": "1", "checks": CHECKS, "probes": [PROBE]},
        "material": MATERIAL,
        **changes,
    }
    return json.dumps(document).encode()


def _facts(report: bytes, **changes: Any) -> ValidationRunFacts:
    values = {
        "validation_job_id": "job-validation-1",
        "job_kind": "voice_validation",
        "job_state": "succeeded",
        "result_json": json.dumps(
            {
                "kind": "voice_validation",
                "validation_report_artifact_id": "report-1",
                "receipt_id": "receipt-1",
                "validation": {
                    "id": "validation-1",
                    "draft_id": "draft-reference",
                    "draft_revision": "1",
                    "validation_set_revision": "1",
                    "model_binding": {"id": "voice-model", "revision": "1"},
                    "status": "passed",
                    "publication_eligible": True,
                    "created_at": "2026-01-01T00:00:00Z",
                    "checks": CHECKS,
                },
            }
        ),
        "draft_id": "draft-reference",
        "draft_revision": "1",
        "draft_kind": "clone",
        "draft_input": {"reference_audio_asset_id": "asset-reference"},
        "binding": {"id": "voice-model", "revision": "1"},
        "binding_revision_id": "voice-model-rev-1",
        "report_artifact_id": "report-1",
        "report_receipt_id": "receipt-1",
        "report_sha256": "sha256:" + __import__("hashlib").sha256(report).hexdigest(),
        "report_bytes": len(report),
        "source_asset": VoiceMaterial.from_value(MATERIAL),
    }
    values.update(changes)
    return ValidationRunFacts(**values)


def test_stable_identities_and_validators() -> None:
    assert evidence_id_for_job("job-1") == "vce-job-1"
    assert voice_id_for_draft("draft-abc") == "voice-abc"
    assert publication_id_for("voice-abc", "1").startswith("vpub-")
    assert revision_id_for("voice-abc", "1").startswith("vrev-")
    assert asset_reference_id("voice-abc", "1", "asset-1").startswith("ref-")
    assert require_identifier("a-b") == "a-b"
    assert require_revision("19") == "19"
    assert require_digest(REPORT_SHA) == REPORT_SHA
    for invalid in ("A", "a", "a b", ""):
        with pytest.raises(ValueError):
            require_identifier(invalid)
    with pytest.raises(ValueError):
        require_revision("0")
    with pytest.raises(ValueError):
        require_digest("sha256:short")


def test_material_round_trip_and_rejections() -> None:
    material = VoiceMaterial.from_value(MATERIAL)
    assert material.as_json() == MATERIAL
    for value in (
        None,
        {},
        {**MATERIAL, "extra": 1},
        {**MATERIAL, "bytes": 0},
        {**MATERIAL, "media_type": ""},
    ):
        with pytest.raises(ValueError):
            VoiceMaterial.from_value(value)


def test_evidence_digest_ignores_observation_time_and_checks_facts() -> None:
    report = _report()
    facts = _facts(report)
    first = build_evidence(facts, report, measured_by="worker", measured_at_us=1)
    second = build_evidence(facts, report, measured_by="worker", measured_at_us=99)
    assert first.evidence_digest == second.evidence_digest
    assert first.synthesis_path == "reusable_tts"
    assert first.as_row()["material"] == MATERIAL
    changed = build_evidence(facts, report, measured_by="other", measured_at_us=1)
    assert changed.evidence_digest != first.evidence_digest
    with pytest.raises(ValueError):
        replace(first, capabilities={})


def test_parse_report_rejects_bad_bytes_and_documents() -> None:
    assert parse_validation_report(b'{"x":1}') == {"x": 1}
    for raw in (b"", b"[]", b"{", b'{"a":1,"a":2}', b'{"a":NaN}', b"\xff"):
        with pytest.raises(VoiceReportUnavailableError):
            parse_validation_report(raw)


def test_validate_capabilities_accepts_and_rejects() -> None:
    assert validate_capabilities(CAPABILITIES) == CAPABILITIES
    for value in (None, {}, {"languages": ["xx"]}, {**CAPABILITIES, "extra": 1}):
        with pytest.raises(VoiceEvidenceNotEligibleError):
            validate_capabilities(value)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"job_kind": "voice_clone"}, "validation_job_not_succeeded"),
        ({"job_state": "running"}, "validation_job_not_succeeded"),
        ({"result_json": None}, "validation_result_missing"),
        ({"result_json": "{"}, "validation_result_invalid"),
        ({"result_json": "[]"}, "validation_result_invalid"),
        ({"report_artifact_id": "other"}, "validation_report_mismatch"),
        ({"report_receipt_id": "other"}, "validation_report_mismatch"),
        ({"binding": {"id": "other", "revision": "1"}}, "validation_binding_mismatch"),
    ],
)
def test_committed_validation_rejections(change: dict[str, Any], reason: str) -> None:
    facts = _facts(_report(), **change)
    with pytest.raises(VoiceEvidenceNotEligibleError) as error:
        committed_validation(facts)
    assert str(error.value) == reason


def test_build_evidence_rejections_are_stable() -> None:
    report = _report()
    cases = [
        ({"measured_by": "bad\tname", "measured_at_us": 1}, {}, "invalid_measured_by"),
        (
            {"measured_by": "worker", "measured_at_us": 1},
            {"report_bytes": 1},
            "report_bytes_mismatch",
        ),
        (
            {"measured_by": "worker", "measured_at_us": 1},
            {"report_sha256": REPORT_SHA},
            "report_bytes_mismatch",
        ),
    ]
    for kwargs, change, reason in cases:
        with pytest.raises(VoiceEvidenceNotEligibleError) as error:
            build_evidence(
                _facts(report, **cast(dict[str, Any], change)),
                report,
                measured_by=cast(str, kwargs["measured_by"]),
                measured_at_us=cast(int, kwargs["measured_at_us"]),
            )
        assert str(error.value) == reason
    for document, reason in (
        (_report(synthesis_path="other"), "synthesis_path_not_reusable"),
        (_report(capabilities={}), "capabilities_invalid"),
        (_report(coverage=None), "coverage_missing"),
        (_report(material=None), "clone_material_missing"),
        (_report(material={**MATERIAL, "asset_id": "other"}), "clone_material_mismatch"),
    ):
        with pytest.raises(VoiceEvidenceNotEligibleError) as error:
            build_evidence(_facts(document), document, measured_by="worker", measured_at_us=1)
        assert str(error.value) == reason


def test_publication_request_parse() -> None:
    request = PublicationRequest.parse(
        {
            "expected_draft_revision": "1",
            "validation_job_id": "job-validation-1",
            "manual_confirmation": True,
            "display_name": "Voice",
        }
    )
    assert request.as_intent()["display_name"] == "Voice"
    for payload in (
        {"extra": 1},
        {"expected_draft_revision": "0"},
        {"expected_draft_revision": "1", "validation_job_id": "bad id"},
        {
            "expected_draft_revision": "1",
            "validation_job_id": "job-1",
            "manual_confirmation": False,
        },
        {
            "expected_draft_revision": "1",
            "validation_job_id": "job-1",
            "manual_confirmation": True,
            "display_name": "",
        },
    ):
        with pytest.raises(VoicePublicationInputError):
            PublicationRequest.parse(payload)


def test_publication_projection_and_guards() -> None:
    evidence = build_evidence(_facts(_report()), _report(), measured_by="worker", measured_at_us=1)
    document = build_voice_revision_document(
        voice_id="voice-reference",
        revision="1",
        display_name="Voice",
        source_type="cloned",
        draft_id="draft-reference",
        draft_revision="1",
        material=VoiceMaterial.from_value(MATERIAL),
        evidence=evidence,
        binding_id="voice-model",
        binding_revision="1",
        checked_at_us=0,
        published_at_us=0,
    )
    assert document["source"]["type"] == "cloned"
    assert document["published_at"] == "1970-01-01T00:00:00Z"
    view = build_publication_view(
        publication_id="vpub-1",
        draft_id="draft-reference",
        draft_revision="1",
        voice_id="voice-reference",
        voice_revision="1",
        validation_job_id="job-validation-1",
        validation_id="validation-1",
        published_at_us=0,
    )
    assert view["manual_confirmation"] is True
    with pytest.raises(VoicePublicationNotEligibleError):
        build_voice_revision_document(
            voice_id="voice-reference",
            revision="1",
            display_name="Voice",
            source_type="designed",
            draft_id="draft-reference",
            draft_revision="1",
            material=VoiceMaterial.from_value(MATERIAL),
            evidence=evidence,
            binding_id="voice-model",
            binding_revision="1",
            checked_at_us=0,
            published_at_us=0,
        )
    with pytest.raises(VoicePublicationNotEligibleError):
        build_voice_revision_document(
            voice_id="voice-reference",
            revision="1",
            display_name="Voice",
            source_type="bad",
            draft_id="draft-reference",
            draft_revision="1",
            material=None,
            evidence=evidence,
            binding_id="voice-model",
            binding_revision="1",
            checked_at_us=0,
            published_at_us=0,
        )
    with pytest.raises(VoicePublicationNotEligibleError):
        require_source_eligibility("design")
    require_source_eligibility("clone")
    assert require_revision_text("1") == "1"
    assert stable_voice_id("draft-reference") == "voice-reference"
    assert stable_revision_id("voice-reference", "1").startswith("vrev-")
    assert timestamp(0) == "1970-01-01T00:00:00Z"


def test_checked_at_and_contract_errors() -> None:
    assert build_evidence_checked_at_us({"created_at": "1970-01-01T00:00:00.5Z"}) == 500_000
    for value in ({}, {"created_at": "not-a-time"}, {"created_at": "2026-01-01T00:00:00"}):
        with pytest.raises(VoicePublicationNotEligibleError):
            build_evidence_checked_at_us(value)
    with pytest.raises(VoicePublicationContractError):
        build_publication_view(
            publication_id="bad id",
            draft_id="draft-reference",
            draft_revision="1",
            voice_id="voice-reference",
            voice_revision="1",
            validation_job_id="job-validation-1",
            validation_id="validation-1",
            published_at_us=0,
        )


class _Store:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body, self.created = body, True

    def publish(self, draft_id: str, request: PublicationRequest, **_: Any) -> Any:
        return self.body, self.created


def test_voice_publications_wrapper_revalidates_body() -> None:
    good = build_publication_view(
        publication_id="vpub-1",
        draft_id="draft-reference",
        draft_revision="1",
        voice_id="voice-reference",
        voice_revision="1",
        validation_job_id="job-validation-1",
        validation_id="validation-1",
        published_at_us=0,
    )
    body, created = VoicePublications(_Store(good)).publish(
        "draft-reference",
        {
            "expected_draft_revision": "1",
            "validation_job_id": "job-validation-1",
            "manual_confirmation": True,
        },
        request_id="request-1",
        idempotency_key=None,
    )
    assert body == good and created is True
    with pytest.raises(VoicePublicationContractError):
        VoicePublications(_Store({"bad": "body"})).publish(
            "draft-reference",
            {
                "expected_draft_revision": "1",
                "validation_job_id": "job-validation-1",
                "manual_confirmation": True,
            },
            request_id="request-1",
            idempotency_key=None,
        )


def test_recorder_cli_success_and_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import speechnexus_backend.record_voice_capability_evidence as cli

    report_file = tmp_path / "report.json"
    report_file.write_bytes(_report())
    evidence = build_evidence(_facts(_report()), _report(), measured_by="worker", measured_at_us=1)

    class FakePool:
        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeRecorder:
        def __init__(self, pool: Any, *, timeout_seconds: float) -> None:
            pass

        def record(self, job_id: str, report: bytes, measured_by: str) -> Any:
            return evidence, True

    monkeypatch.setattr(cli, "speech_pool", lambda *args, **kwargs: FakePool())
    monkeypatch.setattr(cli, "PostgresVoiceEvidenceRecorder", FakeRecorder)
    monkeypatch.setenv("SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL", "postgresql://unused")
    cli.main(
        [
            "--schema",
            "speech",
            "--validation-job-id",
            "job-validation-1",
            "--report",
            str(report_file),
        ]
    )

    with pytest.raises(SystemExit) as missing_env:
        monkeypatch.delenv("SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL")
        cli.main(
            [
                "--schema",
                "speech",
                "--validation-job-id",
                "job-validation-1",
                "--report",
                str(report_file),
            ]
        )
    assert missing_env.value.code == 2

    monkeypatch.setenv("SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL", "postgresql://unused")
    missing_file = tmp_path / "missing.json"
    with pytest.raises(SystemExit) as bad_file:
        cli.main(
            [
                "--schema",
                "speech",
                "--validation-job-id",
                "job-validation-1",
                "--report",
                str(missing_file),
            ]
        )
    assert bad_file.value.code == 2

    class FailingRecorder(FakeRecorder):
        def record(self, job_id: str, report: bytes, measured_by: str) -> Any:
            raise VoiceEvidenceNotEligibleError("evidence_missing")

    monkeypatch.setattr(cli, "PostgresVoiceEvidenceRecorder", FailingRecorder)
    with pytest.raises(SystemExit) as bad_run:
        cli.main(
            [
                "--schema",
                "speech",
                "--validation-job-id",
                "job-validation-1",
                "--report",
                str(report_file),
            ]
        )
    assert bad_run.value.code == 4
