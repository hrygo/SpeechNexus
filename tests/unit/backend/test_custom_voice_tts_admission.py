"""Published clone material is the only path that assembles a custom TTS command."""

import json
from typing import Any

import pytest

from speechnexus_backend.speech.infrastructure.public_job_admission import (
    PublicJobAdmissionRejectedError,
    _custom_voice,
)

NOW = 1_800_000_000_000_000
DIGEST = "sha256:" + "a" * 64
VOICE = {"id": "voice-demo", "revision": 1, "revision_id": "voice-rev-1"}


class Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class Cursor:
    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.queries.append((query, parameters))

    def fetchall(self) -> list[dict[str, Any]]:
        query = self.queries[-1][0]
        for marker, rows in self.responses.items():
            if marker in query:
                return rows
        raise AssertionError(f"unexpected query: {query}")


def _revision(**changes: Any) -> dict[str, Any]:
    value = {
        "source_type": "cloned",
        "material": {
            "asset_id": "synthetic-reference",
            "sha256": DIGEST,
            "bytes": 64,
            "media_type": "audio/wav",
        },
        "draft_id": "draft-1",
        "draft_revision": "1",
        "document": {"model_binding": {"id": "binding-test", "revision": "1"}},
    }
    value.update(changes)
    return value


def _asset(**changes: Any) -> dict[str, Any]:
    value = {
        "asset_state": "ready",
        "purpose": "voice_reference",
        "object_key": "private/reference.wav",
        "sha256": DIGEST,
        "bytes": 64,
        "expires_at_us": NOW + 1,
    }
    value.update(changes)
    return value


def _draft(**changes: Any) -> dict[str, Any]:
    value = {
        "input_json": json.dumps(
            {
                "reference_audio_asset_id": "synthetic-reference",
                "source_use_confirmed": True,
                "reference_text": "Synthetic reference.",
            }
        )
    }
    value.update(changes)
    return value


def _run(
    *,
    revisions: list[dict[str, Any]] | None = None,
    assets: list[dict[str, Any]] | None = None,
    drafts: list[dict[str, Any]] | None = None,
) -> Any:
    cursor = Cursor(
        {
            "speech_voice_revisions": revisions if revisions is not None else [_revision()],
            "speech_assets": assets if assets is not None else [_asset()],
            "speech_voice_draft_revisions": drafts if drafts is not None else [_draft()],
        }
    )
    return _custom_voice(
        cursor,
        VOICE,
        workspace_id="ws-demo",
        binding_id="binding-test",
        binding_revision="1",
        storage_bucket="synthetic",
        now_us=NOW,
    )


def test_a_published_clone_resolves_material_and_its_paired_transcript() -> None:
    facts = _run()
    assert facts is not None
    assert facts.reference_text == "Synthetic reference."
    assert facts.voice_material == {
        "asset_or_artifact_id": "synthetic-reference",
        "workspace_id": "ws-demo",
        "storage_bucket": "synthetic",
        "object_key": "private/reference.wav",
        "content_sha256": DIGEST,
        "byte_length": 64,
    }


def test_a_builtin_publication_has_no_clone_revision() -> None:
    assert _run(revisions=[]) is None


@pytest.mark.parametrize("source_type", ["designed"])
def test_a_design_revision_is_never_accepted_as_a_custom_voice(source_type: str) -> None:
    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        _run(revisions=[_revision(source_type=source_type)])
    assert raised.value.code == "voice_not_published"


def test_a_voice_published_on_another_binding_cannot_run_on_this_tts_binding() -> None:
    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        _run(
            revisions=[
                _revision(document={"model_binding": {"id": "customvoice", "revision": "1"}})
            ]
        )
    assert raised.value.code == "model_binding_task_not_supported"


@pytest.mark.parametrize(
    "input_json",
    [
        json.dumps(
            {"reference_audio_asset_id": "synthetic-reference", "source_use_confirmed": True}
        ),
        json.dumps({"reference_text": ""}),
        json.dumps({"reference_text": "x" * 10001}),
    ],
)
def test_a_published_clone_without_a_usable_transcript_is_refused(input_json: str) -> None:
    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        _run(drafts=[{"input_json": input_json}])
    assert raised.value.code == "job_binding_incomplete"


@pytest.mark.parametrize(
    "asset",
    [
        _asset(asset_state="pending"),
        _asset(purpose="asr_input"),
        _asset(sha256="sha256:" + "b" * 64),
        _asset(bytes=65),
        _asset(expires_at_us=NOW),
    ],
)
def test_material_the_workspace_can_no_longer_use_is_not_published(
    asset: dict[str, Any],
) -> None:
    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        _run(assets=[asset])
    assert raised.value.code == "voice_not_published"


def test_missing_material_row_is_not_a_storage_outage() -> None:
    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        _run(assets=[])
    assert raised.value.code == "voice_not_published"
