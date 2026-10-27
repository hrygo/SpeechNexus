"""Contract-shaped public Artifact metadata projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from speechnexus_contracts import parse
from speechnexus_contracts.models import Artifact

from speechnexus_backend.speech.domain.artifacts import ArtifactRecord


def _timestamp(microseconds: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def public_artifact(artifact: ArtifactRecord) -> Artifact:
    wire: dict[str, Any] = {
        "id": artifact.artifact_id,
        "object": "nexus.artifact",
        "workspace_id": artifact.workspace_id,
        "kind": artifact.kind,
        "status": artifact.status,
        "media_type": artifact.media_type,
        "bytes": artifact.bytes,
        "sha256": artifact.sha256,
        "created_at": _timestamp(artifact.created_at_us),
    }
    if artifact.receipt_id is not None:
        wire["receipt_id"] = artifact.receipt_id
    if artifact.source_operation_id is not None:
        wire["source_operation_id"] = artifact.source_operation_id
    if artifact.expires_at_us is not None:
        wire["expires_at"] = _timestamp(artifact.expires_at_us)
    return parse("Artifact", wire)


__all__ = ["public_artifact"]
