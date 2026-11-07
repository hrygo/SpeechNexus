"""Recorder transaction shape: bounded reads, replay, and stable refusals."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.voice_capability_evidence import (
    VoiceEvidenceNotEligibleError,
    VoiceEvidenceUnavailableError,
    VoiceReportUnavailableError,
)
from speechnexus_backend.speech.infrastructure.voice_capability_evidence_postgres import (
    PostgresVoiceEvidenceRecorder,
    _json_object,
    _material,
)

DIGEST = "sha256:" + "b" * 64
CAPABILITIES = {
    "languages": ["en"],
    "sample_rates_hz": [24000],
    "audio_formats": ["wav"],
    "modes": ["complete"],
}
MATERIAL = {"asset_id": "asset-1", "sha256": DIGEST, "bytes": 64, "media_type": "audio/wav"}
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
    "sample_frames": 12000,
    "output_rms": 2000,
    "output_peak": 12000,
    "output_sha256": "sha256:" + "d" * 64,
    "output_bytes": 24044,
    "mode": "complete",
}
REPORT = json.dumps(
    {
        "synthesis_path": "reusable_tts",
        "capabilities": CAPABILITIES,
        "coverage": {
            "validation_set_revision": "1",
            "checks": [
                {"name": "new_text_reuse", "required": True, "status": "passed"},
                {"name": "reusable_tts", "required": True, "status": "passed"},
            ],
            "probes": [PROBE],
        },
        "material": MATERIAL,
    }
).encode()
VALIDATION = {
    "id": "validation-1",
    "draft_id": "draft-1",
    "draft_revision": "1",
    "validation_set_revision": "1",
    "status": "passed",
    "publication_eligible": True,
    "created_at": "2026-01-01T00:00:00Z",
    "checks": [
        {"name": "new_text_reuse", "required": True, "status": "passed"},
        {"name": "reusable_tts", "required": True, "status": "passed"},
    ],
    "model_binding": {"id": "voice-model", "revision": "1"},
}
JOB = {
    "job_id": "job-1",
    "kind": "voice_validation",
    "state": "succeeded",
    "result_json": json.dumps(
        {
            "kind": "voice_validation",
            "validation": VALIDATION,
            "validation_report_artifact_id": "report-1",
            "receipt_id": "receipt-1",
        }
    ),
    "workspace_id": "workspace-default",
}


class _Cursor:
    def __init__(self, results: list[list[dict[str, Any]]], rowcount: int = 1) -> None:
        self.results = list(results)
        self.rowcount = rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.results.pop(0)


def _results() -> list[list[dict[str, Any]]]:
    return [
        [JOB],
        [
            {
                "kind": "clone",
                "input_json": '{"reference_audio_asset_id":"asset-1"}',
                "binding_json": '{"id":"voice-model","revision":"1"}',
            }
        ],
        [{"revision_id": "voice-model-rev-1"}],
        [
            {
                "artifact_state": "ready",
                "artifact_kind": "validation_report",
                "bytes": len(REPORT),
                "sha256": "sha256:" + __import__("hashlib").sha256(REPORT).hexdigest(),
                "receipt_id": "receipt-1",
            }
        ],
        [
            {
                "asset_id": "asset-1",
                "asset_state": "ready",
                "bytes": 64,
                "sha256": DIGEST,
                "media_type": "audio/wav",
            }
        ],
        [{"proof": 1}],
        [{"t": 10}],
    ]


def _record(cursor: _Cursor) -> Any:
    return PostgresVoiceEvidenceRecorder.__new__(PostgresVoiceEvidenceRecorder)._record(
        cast(Any, cursor), "job-1", REPORT, "worker"
    )


def test_json_and_material_helpers() -> None:
    assert _json_object(None) is None
    assert _json_object("[]") is None
    assert _json_object("{") is None
    assert _json_object('{"a":1}') == {"a": 1}
    assert (
        _material(
            {"asset_id": "asset-1", "sha256": DIGEST, "bytes": 64, "media_type": "audio/wav"}
        ).asset_id
        == "asset-1"
    )


def test_record_inserts_then_replays() -> None:
    evidence, created = _record(_Cursor(_results(), rowcount=1))
    assert created is True and evidence.validation_job_id == "job-1"
    row = evidence.as_row()
    results = _results()
    results.append([row])
    replayed, created = _record(_Cursor(results, rowcount=0))
    assert created is False and replayed.evidence_digest == evidence.evidence_digest


@pytest.mark.parametrize(
    ("results", "error"),
    [
        ([[]], VoiceReportUnavailableError),
        ([[{**JOB, "kind": "voice_clone"}]], VoiceEvidenceNotEligibleError),
        ([[{**JOB, "result_json": "not-json"}]], VoiceEvidenceNotEligibleError),
        (
            [[{**JOB, "result_json": '{"kind":"voice_validation","validation":[]}'}]],
            VoiceEvidenceNotEligibleError,
        ),
    ],
)
def test_job_refusals(results: list[list[dict[str, Any]]], error: type[Exception]) -> None:
    with pytest.raises(error):
        _record(_Cursor(results))


def test_downstream_refusals() -> None:
    for index in range(1, 6):
        results = _results()
        results[index] = []
        with pytest.raises((VoiceEvidenceNotEligibleError, VoiceReportUnavailableError)):
            _record(_Cursor(results))


def test_unavailable_and_conflict_paths() -> None:
    results = _results()
    results[6] = []
    with pytest.raises(VoiceEvidenceUnavailableError):
        _record(_Cursor(results))
    from speechnexus_backend.speech.infrastructure.voice_capability_evidence_postgres import (
        _Cursor as RealCursor,
    )

    class Dead:
        deadline = 0

    with pytest.raises(VoiceEvidenceUnavailableError):
        RealCursor(cast(Any, Dead()), 0).execute("SELECT 1")
