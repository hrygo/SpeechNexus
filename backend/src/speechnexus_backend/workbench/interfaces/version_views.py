"""Domain-to-generated JSON projections of caller-loaded visible, authoritative snapshots.

No HTTP route, pagination cursor, deletion barrier or source qualification is implied by mapping.
"""

from datetime import UTC, datetime

import speechnexus_contracts.workbench_models as wire
from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.domain.listings import TranscriptSummary
from speechnexus_backend.workbench.domain.versions import (
    Project,
    SpeechSource,
    Transcript,
    TranscriptRevision,
    validate_revision_number,
)


def _invalid() -> ValueError:
    return ValueError("invalid workbench version snapshot")


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.utcoffset() is None:
        raise _invalid()
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _revision(value: int) -> str:
    validate_revision_number(value)
    return str(value)


def _source(value: SpeechSource | None) -> dict[str, str | None]:
    if value is not None and (
        type(value) is not SpeechSource
        or type(value.artifact_id) is not str
        or type(value.receipt_id) is not str
    ):
        raise _invalid()
    return {
        "source_artifact_id": value.artifact_id if value is not None else None,
        "source_receipt_id": value.receipt_id if value is not None else None,
    }


def project_payload(value: Project) -> wire.Project:
    if type(value) is not Project:
        raise _invalid()
    validate_revision_number(value.metadata_revision)
    created, updated = _utc(value.created_at), _utc(value.updated_at)
    if updated < created:
        raise _invalid()
    return parse(
        "Project",
        {
            "id": value.id,
            "object": "nexus.workbench_project",
            "workspace_id": value.workspace_id,
            "name": value.name,
            "description": value.description,
            "status": value.status,
            "metadata_revision": value.metadata_revision,
            "created_at": _time(created),
            "updated_at": _time(updated),
        },
    )


def transcript_payload(value: Transcript | TranscriptSummary) -> wire.Transcript:
    if type(value) is Transcript:
        if type(value.head) is not TranscriptRevision or value.head.transcript_id != value.id:
            raise _invalid()
        summary = TranscriptSummary.from_transcript(value)
    elif type(value) is TranscriptSummary:
        summary = value
    else:
        raise _invalid()
    created, updated = _utc(summary.created_at), _utc(summary.updated_at)
    if updated < created:
        raise _invalid()
    return parse(
        "Transcript",
        {
            "id": summary.id,
            "object": "nexus.transcript",
            "project_id": summary.project_id,
            "display_name": summary.display_name,
            "head_revision": _revision(summary.head_revision),
            **_source(summary.source),
            "created_at": _time(created),
            "updated_at": _time(updated),
        },
    )


def revision_payload(value: TranscriptRevision) -> wire.TranscriptRevision:
    if type(value) is not TranscriptRevision:
        raise _invalid()
    number = _revision(value.number)
    source = _revision(value.source_revision) if value.source_revision is not None else None
    if value.source_revision is not None and value.source_revision >= value.number:
        raise _invalid()
    return parse(
        "TranscriptRevision",
        {
            "id": value.id,
            "object": "nexus.transcript_revision",
            "transcript_id": value.transcript_id,
            "revision": number,
            "status": value.status,
            "text": value.text,
            "source_revision": source,
            **_source(value.source),
            "created_at": _time(value.created_at),
        },
    )


def transcript_view(
    value: Transcript, revision: TranscriptRevision | None = None
) -> wire.TranscriptView:
    if type(value) is not Transcript:
        raise _invalid()
    metadata = transcript_payload(value)
    selected = value.head if revision is None else revision
    selected_payload = revision_payload(selected)
    # Compare the same normalized instants emitted on the wire, including DST folds.
    selected_time = datetime.fromisoformat(selected_payload["created_at"])
    created_time = datetime.fromisoformat(metadata["created_at"])
    head_time = datetime.fromisoformat(metadata["updated_at"])
    if (
        selected.transcript_id != value.id
        or selected.number > value.head.number
        or selected.source != value.head.source
        or selected_time < created_time
        or selected_time > head_time
        or (selected.number == value.head.number and selected != value.head)
    ):
        raise _invalid()
    return parse(
        "TranscriptView",
        {
            "object": "nexus.transcript_view",
            "transcript": metadata,
            "revision": selected_payload,
        },
    )
