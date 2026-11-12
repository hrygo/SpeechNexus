"""The draft read projection of a published draft and its fail-closed edges."""

import json
from typing import Any

import pytest
from speechnexus_contracts.voice_drafts_http import validate_response

from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.infrastructure.voice_drafts_postgres import _view

NOW = 1_700_000_000_000_000


def _row(**changes: Any) -> dict[str, Any]:
    row = {
        "draft_id": "draft-reference",
        "kind": "clone",
        "status": "published",
        "head_revision": "1",
        "metadata_revision": 2,
        "display_name": "Voice reference",
        "created_at_us": NOW,
        "updated_at_us": NOW,
        "revision": "1",
        "generation_job_id": "job-generation-1",
        "input_json": json.dumps(
            {
                "kind": "clone",
                "reference_audio_asset_id": "asset-reference",
                "source_use_confirmed": True,
            }
        ),
        "binding_json": json.dumps({"id": "voice-model", "revision": "1"}),
        "revision_created_at_us": NOW,
        "job_state": "succeeded",
        "result_json": json.dumps(
            {
                "kind": "voice_clone",
                "voice_draft_id": "draft-reference",
                "draft_revision": "1",
                "preview_artifact_ids": ["artifact-preview-1"],
            }
        ),
        "error_code": None,
        "job_workspace_id": "workspace-default",
        "job_updated_at_us": NOW,
        "published_voice_id": "voice-reference",
        "published_voice_revision": "1",
        "validation_job_id": "job-validation-1",
    }
    row.update(changes)
    return row


def test_published_view_projects_the_publication_facts() -> None:
    view = _view(_row())
    # The body the API actually answers with must satisfy the declared 200; before
    # the projection this raised ``ContractValidationError`` and became a 500.
    validate_response("getVoiceDraft", 200, {"body": view})
    assert view["draft"]["status"] == "published"
    revision = view["revision"]
    assert revision["status"] == "published"
    assert revision["validation_job_id"] == "job-validation-1"
    assert revision["published_voice_id"] == "voice-reference"
    assert revision["published_voice_revision"] == "1"
    # A published revision still owes the previews its generation Job produced.
    assert revision["preview_artifact_ids"] == ["artifact-preview-1"]


def test_unpublished_view_does_not_invent_publication_fields() -> None:
    view = _view(_row(status="generating", published_voice_id=None, published_voice_revision=None))
    assert view["revision"]["status"] == "candidate"
    assert "validation_job_id" not in view["revision"]
    assert "published_voice_id" not in view["revision"]
    assert "published_voice_revision" not in view["revision"]


@pytest.mark.parametrize(
    "changes",
    [
        {"published_voice_id": None},
        {"published_voice_revision": None},
        {"validation_job_id": None},
        {"job_state": "running"},
        {"job_state": "failed"},
        {"result_json": json.dumps({"kind": "voice_clone"})},
        {"result_json": json.dumps({"not": "a generation result"})},
        {"result_json": "not json"},
    ],
    ids=[
        "no-voice-identity",
        "no-voice-revision",
        "no-validation-job",
        "generation-running",
        "generation-failed",
        "no-preview-facts",
        "foreign-result",
        "unparsable-result",
    ],
)
def test_published_view_fails_closed_instead_of_violating_the_contract(
    changes: dict[str, Any],
) -> None:
    """Missing publication facts are a storage outage (503), never a bad body (500)."""
    with pytest.raises(PublicJobUnavailableError):
        _view(_row(**changes))
