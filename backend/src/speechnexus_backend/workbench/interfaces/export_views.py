"""Private export snapshot projected into the generated wire shape.

The wire job carries the manifest it pins, never the private unit selection that
produced it: that selection stays inside the export's own rows.
"""

from datetime import datetime
from typing import Any, cast

from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.domain.exports import ExportJob


def _time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def export_job_payload(value: ExportJob) -> Any:
    if type(value) is not ExportJob:
        raise ValueError("invalid export job")
    published = value.artifact
    return parse(
        "ExportJob",
        cast(
            Any,
            {
                "object": "nexus.export_job",
                "id": value.id,
                "book_id": value.book_id,
                "status": value.status,
                "format": value.format,
                "manifest_revision": str(value.manifest_revision),
                "progress_completed": value.progress_completed,
                "progress_total": value.progress_total,
                "artifact": None
                if published is None
                else {
                    "object": "nexus.export_artifact",
                    "id": published.id,
                    "export_id": published.export_id,
                    "manifest_revision": str(published.manifest_revision),
                    "media_type": published.media_type,
                    "bytes": published.bytes,
                    "sha256": published.sha256,
                    "created_at": _time(published.created_at),
                },
                "error": None
                if value.error is None
                else {
                    "code": value.error.code,
                    "message": value.error.message,
                    "retryable": value.error.retryable,
                },
                "created_at": _time(value.created_at),
                "updated_at": _time(value.updated_at),
            },
        ),
    )
