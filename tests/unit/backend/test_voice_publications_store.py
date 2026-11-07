"""Publication transaction guard order, exercised without a database."""

from __future__ import annotations

from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.voice_publications import (
    PublicationRequest,
    VoicePublicationConflictError,
    VoicePublicationNotEligibleError,
    VoicePublicationNotFoundError,
)
from speechnexus_backend.speech.domain.voice_capability_evidence import (
    VoiceCapabilityEvidence,
)
from speechnexus_backend.speech.infrastructure.voice_publications_postgres import (
    PostgresVoicePublications,
    _evidence_view,
    _material,
)

REQUEST = PublicationRequest("1", "job-validation-1", True, None)


class _Cursor:
    def __init__(self, results: list[list[dict[str, Any]]]) -> None:
        self.results = list(results)
        self.rowcount = 1

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.results.pop(0)


def _publish(results: list[list[dict[str, Any]]], *, key: str | None = None) -> Any:
    return PostgresVoicePublications.__new__(PostgresVoicePublications)._publish(
        cast(Any, _Cursor(results)),
        "draft-1",
        REQUEST,
        request_digest="sha256:" + "a" * 64,
        key_digest=key,
    )


def test_missing_draft_is_not_found() -> None:
    with pytest.raises(VoicePublicationNotFoundError):
        _publish([[]])


def test_key_replay_returns_the_receipt_before_head_checks() -> None:
    body = {"publication_id": "vpub-1"}
    digest = "sha256:" + "a" * 64
    result, created = _publish(
        [
            [
                {
                    "draft_id": "draft-1",
                    "kind": "clone",
                    "status": "published",
                    "head_revision": "9",
                    "display_name": "Voice",
                }
            ],
            [{"request_digest": digest, "result": body}],
        ],
        key="sha256:" + "b" * 64,
    )
    assert result == body and created is False


def test_stale_head_and_lifecycle_conflicts() -> None:
    draft = {
        "draft_id": "draft-1",
        "kind": "clone",
        "status": "candidate",
        "head_revision": "2",
        "display_name": "Voice",
    }
    with pytest.raises(VoicePublicationConflictError):
        _publish([[draft], []])
    draft["head_revision"] = "1"
    draft["status"] = "published"
    with pytest.raises(VoicePublicationConflictError):
        _publish([[draft], []])


def test_design_is_rejected_before_reading_evidence() -> None:
    draft = {
        "draft_id": "draft-1",
        "kind": "design",
        "status": "candidate",
        "head_revision": "1",
        "display_name": "Voice",
    }
    with pytest.raises(VoicePublicationNotEligibleError) as error:
        _publish([[draft], []])
    assert str(error.value) == "design_preview_only"


def test_existing_publication_replays_after_reading_the_draft() -> None:
    draft = {
        "draft_id": "draft-1",
        "kind": "clone",
        "status": "candidate",
        "head_revision": "1",
        "display_name": "Voice",
    }
    stored = {
        "voice_id": "voice-1",
        "revision": "1",
        "revision_id": "vrev-1",
        "document": {},
        "validation_job_id": "job-validation-1",
        "validation_id": "validation-1",
        "published_at_us": 0,
    }
    body, created = _publish([[draft], [stored]])
    assert created is False and body["voice_revision"] == "1"


def test_evidence_and_material_helpers() -> None:
    row: dict[str, Any] = {
        "evidence_id": "vce-job-validation-1",
        "draft_id": "draft-1",
        "draft_revision": "1",
        "validation_job_id": "job-validation-1",
        "validation_id": "validation-1",
        "binding_revision_id": "binding-rev-1",
        "report_artifact_id": "report-1",
        "report_sha256": "sha256:" + "c" * 64,
        "report_receipt_id": "receipt-1",
        "capabilities": {
            "languages": ["en"],
            "sample_rates_hz": [24000],
            "audio_formats": ["wav"],
            "modes": ["complete"],
        },
        "coverage": {"synthesis_path": "reusable_tts"},
        "material": {
            "asset_id": "asset-1",
            "sha256": "sha256:" + "d" * 64,
            "bytes": 1,
            "media_type": "audio/wav",
        },
        "evidence_digest": "sha256:" + "e" * 64,
        "measured_by": "worker",
        "measured_at_us": 1,
    }
    row["evidence_digest"] = VoiceCapabilityEvidence.compute_digest(
        draft_id="draft-1",
        draft_revision="1",
        validation_job_id="job-validation-1",
        validation_id="validation-1",
        binding_revision_id="binding-rev-1",
        report_artifact_id="report-1",
        report_sha256="sha256:" + "c" * 64,
        report_receipt_id="receipt-1",
        evidence_kind="validation_run",
        measured_by="worker",
        capabilities=row["capabilities"],
        coverage=row["coverage"],
        material=row["material"],
    )
    evidence = _evidence_view(row)
    assert evidence.material is not None and evidence.material.asset_id == "asset-1"
    material = _material(
        {
            "material": {
                "asset_id": "asset-1",
                "sha256": "sha256:" + "d" * 64,
                "bytes": 1,
                "media_type": "audio/wav",
            }
        }
    )
    assert material is not None
    assert material.bytes == 1
    row["material"] = None
    row["evidence_digest"] = VoiceCapabilityEvidence.compute_digest(
        draft_id="draft-1",
        draft_revision="1",
        validation_job_id="job-validation-1",
        validation_id="validation-1",
        binding_revision_id="binding-rev-1",
        report_artifact_id="report-1",
        report_sha256="sha256:" + "c" * 64,
        report_receipt_id="receipt-1",
        evidence_kind="validation_run",
        measured_by="worker",
        capabilities=row["capabilities"],
        coverage=row["coverage"],
        material=None,
    )
    assert _evidence_view(row).material is None
