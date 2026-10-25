"""Domain snapshots become generated wire models, with explicit revision/source association."""

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.domain.listings import TranscriptSummary
from speechnexus_backend.workbench.domain.versions import Project, SpeechSource, Transcript
from speechnexus_backend.workbench.interfaces.version_views import (
    project_payload,
    revision_payload,
    transcript_payload,
    transcript_view,
)

NOW = datetime(2026, 10, 24, 8, tzinfo=timezone(timedelta(hours=8)))
LATER = NOW + timedelta(seconds=1)


def original(*, speech: bool = False) -> Transcript:
    return Transcript.create(
        transcript_id="transcript-one",
        project_id="project-one",
        display_name="  合成稿  ",
        revision_id="revision-one",
        text=" e\u0301\n😀 ",
        now=NOW,
        source=SpeechSource("artifact-one", "receipt-one") if speech else None,
    )


def test_project_uses_generated_shape_integer_metadata_revision_nullable_description_and_utc() -> (
    None
):
    value = Project.create(
        project_id="project-one", workspace_id="workspace-one", name="  合成项目  ", now=NOW
    )
    result = project_payload(value)
    assert result == parse(
        "Project",
        {
            "id": "project-one",
            "object": "nexus.workbench_project",
            "workspace_id": "workspace-one",
            "name": "  合成项目  ",
            "description": None,
            "status": "active",
            "metadata_revision": 1,
            "created_at": "2026-10-24T00:00:00Z",
            "updated_at": "2026-10-24T00:00:00Z",
        },
    )
    assert type(result["metadata_revision"]) is int
    assert not any(key in result for key in ("head", "profile", "credentials", "internal"))


@pytest.mark.parametrize("speech", [False, True])
def test_transcript_and_summary_publish_metadata_only_and_keep_paired_source(speech: bool) -> None:
    value = original(speech=speech)
    result = transcript_payload(value)
    assert result == transcript_payload(TranscriptSummary.from_transcript(value))
    assert result["head_revision"] == "1" and result["display_name"] == "  合成稿  "
    assert "text" not in result and "head" not in result and "source" not in result
    assert (result["source_artifact_id"], result["source_receipt_id"]) == (
        ("artifact-one", "receipt-one") if speech else (None, None)
    )
    assert parse("Transcript", result) == result


def test_revision_unicode_empty_content_and_largest_decimal_revision_are_lossless() -> None:
    value = original(speech=True).head
    result = revision_payload(value)
    assert result["text"] == " e\u0301\n😀 " and result["revision"] == "1"
    assert result["source_revision"] is None and result["status"] == "sealed"
    empty = replace(
        value, id="revision-max", number=10**19 - 1, source_revision=10**19 - 2, text=""
    )
    maximum = revision_payload(empty)
    assert maximum["revision"] == "9999999999999999999"
    assert maximum["source_revision"] == "9999999999999999998" and maximum["text"] == ""
    assert parse("TranscriptRevision", maximum) == maximum


def test_historical_view_keeps_selected_revision_and_current_metadata_head() -> None:
    first = original(speech=True)
    current = first.revise(
        expected_revision=1, revision_id="revision-two", text="校对稿", now=LATER
    )
    result = transcript_view(current, first.head)
    assert result["object"] == "nexus.transcript_view"
    assert result["transcript"]["head_revision"] == "2"
    assert result["transcript"]["updated_at"] == "2026-10-24T00:00:01Z"
    assert result["revision"]["revision"] == "1" and result["revision"]["text"] == first.head.text
    assert transcript_view(current)["revision"]["text"] == "校对稿"
    assert parse("TranscriptView", result) == result
    before = copy.deepcopy(result)
    result["revision"]["text"] = "调用方修改"
    assert transcript_view(current, first.head) == before
    assert first.head.text == " e\u0301\n😀 "


def test_cross_resource_future_head_forgery_wrong_source_or_time_cannot_form_view() -> None:
    first = original(speech=True)
    current = first.revise(
        expected_revision=1, revision_id="revision-two", text="校对稿", now=LATER
    )
    for selected in (
        replace(first.head, transcript_id="another-transcript"),
        replace(first.head, number=3),
        replace(current.head, id="forged-head"),
        replace(current.head, text="伪造head正文"),
        replace(first.head, source=SpeechSource("artifact-other", "receipt-other")),
        replace(first.head, created_at=NOW - timedelta(seconds=1)),
        replace(first.head, created_at=LATER + timedelta(seconds=1)),
    ):
        with pytest.raises(ValueError) as caught:
            transcript_view(current, selected)
        assert "伪造" not in str(caught.value)


@pytest.mark.parametrize("number", [True, 0, -1, 1.0, "1", 10**19])
def test_bad_domain_revisions_are_not_stringified_into_valid_wire_values(number: Any) -> None:
    value = original()
    with pytest.raises(ValueError):
        revision_payload(replace(value.head, number=number))
    with pytest.raises(ValueError):
        revision_payload(replace(value.head, number=2, source_revision=number))
    with pytest.raises(ValueError):
        transcript_payload(replace(TranscriptSummary.from_transcript(value), head_revision=number))
    with pytest.raises(ValueError):
        project_payload(
            replace(
                Project.create(
                    project_id="project-one", workspace_id="workspace-one", name="项目", now=NOW
                ),
                metadata_revision=number,
            )
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "UPPERCASE"},
        {"status": "private-status"},
        {"text": {}},
        {"source": SpeechSource("artifact-one", "BAD-RECEIPT")},
    ],
)
def test_generated_final_validation_rejects_invalid_internal_revision_fields(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ContractValidationError) as caught:
        revision_payload(replace(original().head, **changes))
    assert "private-status" not in str(caught.value) and "BAD-RECEIPT" not in str(caught.value)


@pytest.mark.parametrize("now", [datetime(2026, 10, 24), None, "2026-10-24T00:00:00Z"])
def test_invalid_time_never_uses_implicit_host_timezone(now: Any) -> None:
    value = original()
    with pytest.raises(ValueError):
        revision_payload(replace(value.head, created_at=now))
    with pytest.raises(ValueError):
        transcript_payload(replace(TranscriptSummary.from_transcript(value), updated_at=now))


def test_invalid_nested_domain_snapshots_fail_safely() -> None:
    value = original()
    invalid = (
        lambda: transcript_payload(
            replace(value, head=replace(value.head, transcript_id="another"))
        ),
        lambda: transcript_payload(
            replace(value, head=replace(value.head, created_at=NOW - timedelta(seconds=1)))
        ),
        lambda: transcript_payload(replace(value, head={})),  # type: ignore[arg-type]
        lambda: transcript_payload({}),  # type: ignore[arg-type]
        lambda: project_payload({}),  # type: ignore[arg-type]
        lambda: revision_payload({}),  # type: ignore[arg-type]
        lambda: revision_payload(replace(value.head, source={})),  # type: ignore[arg-type]
        lambda: revision_payload(replace(value.head, source_revision=1)),
    )
    for action in invalid:
        with pytest.raises(ValueError):
            action()
    assert revision_payload(value.head)["created_at"] == NOW.astimezone(UTC).isoformat().replace(
        "+00:00", "Z"
    )


@pytest.mark.parametrize(
    "source",
    [
        SpeechSource("artifact-one", None),  # type: ignore[arg-type]
        SpeechSource(None, "receipt-one"),  # type: ignore[arg-type]
        SpeechSource(None, None),  # type: ignore[arg-type]
    ],
)
def test_partial_or_empty_pinned_source_cannot_be_downgraded_to_nullable_manual_fields(
    source: Any,
) -> None:
    value = original(speech=True)
    with pytest.raises(ValueError):
        revision_payload(replace(value.head, source=source))
    with pytest.raises(ValueError):
        transcript_payload(replace(value, head=replace(value.head, source=source)))


@pytest.mark.parametrize("kind", ["project", "transcript"])
@pytest.mark.parametrize("valid", [False, True])
def test_dst_fold_metadata_chronology_uses_instants_not_local_wall_time(
    kind: str, valid: bool
) -> None:
    zone = ZoneInfo("America/New_York")
    created = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0 if valid else 1)
    updated = datetime(2026, 11, 1, 1, 15 if valid else 45, tzinfo=zone, fold=1 if valid else 0)

    def render() -> dict[str, Any]:
        if kind == "project":
            value = Project.create(
                project_id="project-one", workspace_id="workspace-one", name="合成项目", now=NOW
            )
            return dict(
                project_payload(
                    replace(value, metadata_revision=2, created_at=created, updated_at=updated)
                )
            )
        summary = TranscriptSummary.from_transcript(original())
        return dict(
            transcript_payload(
                replace(summary, head_revision=2, created_at=created, updated_at=updated)
            )
        )

    if not valid:
        with pytest.raises(ValueError):
            render()
    else:
        result = render()
        assert datetime.fromisoformat(result["created_at"]) < datetime.fromisoformat(
            result["updated_at"]
        )


@pytest.mark.parametrize(
    "creation,selected_at,head_at,valid",
    [
        ((30, 0), (15, 1), (45, 1), True),
        ((15, 1), (30, 0), (45, 1), False),
        ((0, 0), (15, 1), (30, 0), False),
        ((0, 0), (45, 0), (30, 1), True),
    ],
)
def test_dst_fold_historical_view_uses_utc_bounds(
    creation: tuple[int, int], selected_at: tuple[int, int], head_at: tuple[int, int], valid: bool
) -> None:
    zone = ZoneInfo("America/New_York")

    def stamp(value: tuple[int, int]) -> datetime:
        return datetime(2026, 11, 1, 1, value[0], tzinfo=zone, fold=value[1])

    initial = original()
    selected = replace(initial.head, created_at=stamp(selected_at))
    current = replace(
        initial,
        created_at=stamp(creation),
        head=replace(
            initial.head,
            id="revision-two",
            number=2,
            source_revision=1,
            text="当前合成稿",
            created_at=stamp(head_at),
        ),
    )
    if not valid:
        with pytest.raises(ValueError):
            transcript_view(current, selected)
    else:
        result = transcript_view(current, selected)
        assert datetime.fromisoformat(result["transcript"]["created_at"]) <= datetime.fromisoformat(
            result["revision"]["created_at"]
        )
        assert datetime.fromisoformat(result["revision"]["created_at"]) <= datetime.fromisoformat(
            result["transcript"]["updated_at"]
        )
