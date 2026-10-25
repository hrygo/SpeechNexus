"""The export projection must satisfy the wire shape it is declared against."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from speechnexus_contracts.workbench_http import validate_response

from speechnexus_backend.workbench.domain.exports import (
    ExportArtifact,
    ExportError,
    ExportJob,
)
from speechnexus_backend.workbench.interfaces.export_views import export_job_payload

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "ab" * 32


def artifact(**overrides: Any) -> ExportArtifact:
    fields: dict[str, Any] = {
        "id": "artifact_demo",
        "export_id": "export_demo",
        "manifest_revision": 1,
        "media_type": "application/zip",
        "bytes": 2048,
        "sha256": DIGEST,
        "created_at": NOW + timedelta(seconds=5),
    }
    fields.update(overrides)
    return ExportArtifact(**fields)


def job(**overrides: Any) -> ExportJob:
    fields: dict[str, Any] = {
        "export_id": "export_demo",
        "book_id": "book_demo",
        "export_format": "zip",
        "manifest_revision": 1,
        "progress_total": 2,
        "now": NOW,
    }
    fields.update(overrides)
    return ExportJob.create(**fields)


def states() -> list[ExportJob]:
    started = job().start(now=NOW + timedelta(1))
    return [
        job(),
        started.record_progress(completed=1, now=NOW + timedelta(2)),
        started.complete(artifact=artifact(), now=NOW + timedelta(3)),
        started.fail(
            error=ExportError("unavailable", "合成导出失败", True), now=NOW + timedelta(3)
        ),
        started.request_cancel(now=NOW + timedelta(2)).settle_cancel(now=NOW + timedelta(3)),
        started.complete(artifact=artifact(), now=NOW + timedelta(3)).expire(
            now=NOW + timedelta(4)
        ),
    ]


@pytest.mark.parametrize("value", states())
def test_every_state_projects_into_a_contract_valid_job(value: ExportJob) -> None:
    payload = export_job_payload(value)
    validate_response("getBookExport", 200, {"body": payload})
    assert payload["object"] == "nexus.export_job"
    assert payload["manifest_revision"] == "1"
    assert payload["progress_completed"] <= payload["progress_total"]
    # The requested unit selection is private to the export and never travels out.
    assert "unit_ids" not in payload


def test_artifact_and_error_appear_exactly_with_their_states() -> None:
    done = next(state for state in states() if state.status == "succeeded")
    payload = export_job_payload(done)
    assert payload["artifact"] is not None
    assert payload["artifact"]["object"] == "nexus.export_artifact"
    assert payload["artifact"]["sha256"] == DIGEST
    assert payload["artifact"]["manifest_revision"] == "1"
    assert payload["error"] is None

    failed = next(state for state in states() if state.status == "failed")
    failed_payload = export_job_payload(failed)
    assert failed_payload["artifact"] is None
    assert failed_payload["error"] == {
        "code": "unavailable",
        "message": "合成导出失败",
        "retryable": True,
    }

    expired = next(state for state in states() if state.status == "expired")
    # Retention removed the artifact, so the wire must not resurrect it.
    assert export_job_payload(expired)["artifact"] is None


def test_a_foreign_value_is_refused_instead_of_projected() -> None:
    with pytest.raises(ValueError):
        export_job_payload("not an export")  # type: ignore[arg-type]
