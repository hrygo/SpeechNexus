"""Workbench revisions preserve original content and reject lost updates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    Project,
    ProjectChanges,
    RevisionConflictError,
    SpeechSource,
    Transcript,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)

INVALID_TEXT = ["synthetic\x00body", "synthetic\ud800body", "synthetic\udfffbody"]


def project() -> Project:
    return Project.create(
        project_id="project-test",
        workspace_id="workspace-test",
        name="合成项目",
        description="合成说明",
        now=NOW,
    )


def transcript(*, imported: bool = False) -> Transcript:
    return Transcript.create(
        transcript_id="transcript-test",
        project_id="project-test",
        display_name="合成转写",
        revision_id="revision-original",
        text="合成原文",
        now=NOW,
        source=SpeechSource("artifact-test", "receipt-test") if imported else None,
    )


@pytest.mark.parametrize("text", INVALID_TEXT, ids=["nul", "high-surrogate", "low-surrogate"])
@pytest.mark.parametrize(
    "change",
    [
        lambda text: Project.create(project_id="p", workspace_id="w", name=text, now=NOW),
        lambda text: Project.create(
            project_id="p", workspace_id="w", name="valid", description=text, now=NOW
        ),
        lambda text: project().edit(
            expected_revision=1, changes=ProjectChanges(name=text), now=LATER
        ),
        lambda text: project().edit(
            expected_revision=1, changes=ProjectChanges(description=text), now=LATER
        ),
        lambda text: Transcript.create(
            transcript_id="t",
            project_id="p",
            display_name=text,
            revision_id="r",
            text="valid",
            now=NOW,
        ),
        lambda text: Transcript.create(
            transcript_id="t",
            project_id="p",
            display_name="valid",
            revision_id="r",
            text=text,
            now=NOW,
        ),
        lambda text: transcript().revise(
            expected_revision=1, revision_id="r", text=text, now=LATER
        ),
    ],
    ids=["project-name", "project-description", "rename", "describe", "title", "manual", "revise"],
)
def test_content_rejects_nonpersistable_characters_without_echoing_text(
    text: str, change: Callable[[str], object]
) -> None:
    with pytest.raises(InvalidVersionChangeError) as caught:
        change(text)
    assert "synthetic" not in str(caught.value)


@pytest.mark.parametrize("text", [" \t中文\r\n🙂e\u0301 ", "\u0001\u007f\uffff\U0010ffff"])
def test_content_preserves_supported_unicode_and_whitespace(text: str) -> None:
    original = Project.create(
        project_id="p", workspace_id="w", name=text, description=text, now=NOW
    )
    created = Transcript.create(
        transcript_id="t", project_id="p", display_name=text, revision_id="r", text=text, now=NOW
    )
    assert (
        original.name == original.description == created.display_name == created.head.text == text
    )
    assert (
        created.revise(expected_revision=1, revision_id="r2", text=text, now=LATER).head.text
        == text
    )


def test_project_changes_require_current_metadata_revision_and_preserve_original() -> None:
    original = project()
    updated = original.edit(
        expected_revision=1,
        changes=ProjectChanges(name="校对项目"),
        now=LATER,
    )
    assert updated.name == "校对项目"
    assert updated.metadata_revision == 2
    assert updated.description == original.description
    assert updated.created_at == NOW
    assert updated.updated_at == LATER
    assert original.metadata_revision == 1
    assert original.name == "合成项目"
    with pytest.raises(RevisionConflictError):
        updated.edit(expected_revision=1, changes=ProjectChanges(name="迟到修改"), now=LATER)


def test_patch_distinguishes_omitted_description_from_explicit_null() -> None:
    unchanged = project().edit(
        expected_revision=1, changes=ProjectChanges(name="新标题"), now=LATER
    )
    cleared = project().edit(
        expected_revision=1, changes=ProjectChanges(description=None), now=LATER
    )
    assert unchanged.description == "合成说明"
    assert cleared.description is None
    assert cleared.metadata_revision == 2


def test_archive_and_reactivate_are_versioned_metadata_changes() -> None:
    archived = project().edit(
        expected_revision=1, changes=ProjectChanges(status="archived"), now=LATER
    )
    active = archived.edit(expected_revision=2, changes=ProjectChanges(status="active"), now=LATER)
    assert archived.status == "archived"
    assert active.status == "active"
    assert active.metadata_revision == 3


@pytest.mark.parametrize(
    "changes",
    [
        ProjectChanges(),
        ProjectChanges(name=""),
        ProjectChanges(name="x" * 129),
        ProjectChanges(description="x" * 2001),
    ],
)
def test_invalid_project_edit_does_not_change_state(changes: ProjectChanges) -> None:
    original = project()
    with pytest.raises(InvalidVersionChangeError):
        original.edit(expected_revision=1, changes=changes, now=LATER)
    assert original.metadata_revision == 1


def test_manual_source_starts_as_draft_and_correction_can_clear_all_text() -> None:
    original = transcript()
    corrected = original.revise(
        expected_revision=1,
        revision_id="revision-corrected",
        text="",
        now=LATER,
    )
    assert original.head.status == "draft"
    assert original.head.source_revision is None
    assert corrected.head.number == 2
    assert corrected.head.text == ""
    assert corrected.head.source_revision == 1
    assert original.head.text == "合成原文"


def test_imported_source_is_sealed_and_remains_fixed_after_correction() -> None:
    original = transcript(imported=True)
    corrected = original.revise(
        expected_revision=1,
        revision_id="revision-corrected",
        text="合成校对",
        now=LATER,
    )
    assert original.head.status == "sealed"
    assert corrected.head.status == "draft"
    assert corrected.head.source == SpeechSource("artifact-test", "receipt-test")
    assert corrected.head.source_revision == original.head.number
    assert original.head.text == "合成原文"
    assert original.head.created_at == NOW
    with pytest.raises(FrozenInstanceError):
        original.head.text = "覆盖原文"  # type: ignore[misc]


def test_explicit_source_must_belong_to_same_transcript_and_not_be_a_future_revision() -> None:
    original = transcript()
    corrected = original.revise(
        expected_revision=1,
        revision_id="revision-corrected",
        text="合成校对",
        now=LATER,
    )
    third = corrected.revise(
        expected_revision=2,
        revision_id="revision-third",
        text="合成再校对",
        now=LATER,
        source_revision=original.head,
    )
    assert third.head.source_revision == 1
    with pytest.raises(InvalidVersionChangeError):
        original.revise(
            expected_revision=1,
            revision_id="revision-invalid",
            text="x",
            now=LATER,
            source_revision=corrected.head,
        )
    other = Transcript.create(
        transcript_id="transcript-other",
        project_id="project-test",
        display_name="其他",
        revision_id="revision-other",
        text="其他合成文本",
        now=NOW,
    )
    with pytest.raises(InvalidVersionChangeError):
        original.revise(
            expected_revision=1,
            revision_id="revision-invalid",
            text="x",
            now=LATER,
            source_revision=other.head,
        )


def test_transcript_edit_rejects_stale_head() -> None:
    original = transcript()
    corrected = original.revise(
        expected_revision=1, revision_id="revision-new", text="新稿", now=LATER
    )
    with pytest.raises(RevisionConflictError):
        corrected.revise(expected_revision=1, revision_id="revision-late", text="迟到", now=LATER)
    assert corrected.head.number == 2


@pytest.mark.parametrize("text", ["", "x" * 1000001])
def test_manual_creation_rejects_invalid_text(text: str) -> None:
    with pytest.raises(InvalidVersionChangeError):
        Transcript.create(
            transcript_id="transcript-test",
            project_id="project-test",
            display_name="合成",
            revision_id="revision-test",
            text=text,
            now=NOW,
        )


def test_successful_empty_speech_result_can_be_preserved() -> None:
    imported = Transcript.create(
        transcript_id="transcript-test",
        project_id="project-test",
        display_name="合成静音",
        revision_id="revision-test",
        text="",
        now=NOW,
        source=SpeechSource("artifact-test", "receipt-test"),
    )
    assert imported.head.text == ""
    assert imported.head.status == "sealed"


def test_version_times_require_timezone_and_do_not_move_backwards() -> None:
    with pytest.raises(InvalidVersionChangeError):
        project().edit(
            expected_revision=1, changes=ProjectChanges(name="新"), now=NOW - timedelta(seconds=1)
        )
    with pytest.raises(InvalidVersionChangeError):
        transcript().revise(
            expected_revision=1, revision_id="revision-new", text="新", now=NOW.replace(tzinfo=None)
        )


def test_revision_counter_never_overflows_the_public_decimal_range() -> None:
    original = transcript()
    exhausted = replace(original, head=replace(original.head, number=10**19 - 1))
    with pytest.raises(InvalidVersionChangeError, match="range exhausted"):
        exhausted.revise(
            expected_revision=10**19 - 1, revision_id="revision-overflow", text="新", now=LATER
        )


def test_project_counter_never_overflows_the_public_decimal_range() -> None:
    exhausted = replace(project(), metadata_revision=10**19 - 1)
    with pytest.raises(InvalidVersionChangeError, match="range exhausted"):
        exhausted.edit(expected_revision=10**19 - 1, changes=ProjectChanges(name="新"), now=LATER)


def test_correction_rejects_a_source_with_different_artifact_provenance() -> None:
    original = transcript(imported=True)
    wrong_source = replace(original.head, source=SpeechSource("artifact-other", "receipt-other"))
    with pytest.raises(InvalidVersionChangeError):
        original.revise(
            expected_revision=1,
            revision_id="revision-new",
            text="新",
            now=LATER,
            source_revision=wrong_source,
        )


def test_content_does_not_enter_default_object_representations() -> None:
    assert "合成项目" not in repr(project())
    assert "合成说明" not in repr(project())
    assert "合成转写" not in repr(transcript())
    assert "合成原文" not in repr(transcript())
