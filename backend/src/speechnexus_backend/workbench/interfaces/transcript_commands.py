"""Consume generated C1 JSON bodies; HTTP parameters, responses and registration are separate."""

from typing import Any

from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from speechnexus_backend.workbench.domain.versions import Transcript


class TranscriptCommandBoundary:
    """Validate before existing use cases open transactions or read public speech sources."""

    def __init__(self, service: WorkbenchVersions) -> None:
        self._service = service

    async def create_transcript(
        self, project_id: str, request: Any, *, idempotency_key: str | None = None
    ) -> Transcript:
        body = parse("TranscriptCreateRequest", request)
        source = body["source"]
        if source["kind"] == "manual":
            return await self._service.create_manual_transcript(
                project_id=project_id,
                display_name=body["display_name"],
                text=source["text"],
                idempotency_key=idempotency_key,
            )
        return await self._service.import_speech_transcript(
            project_id=project_id,
            display_name=body["display_name"],
            artifact_id=source["artifact_id"],
            receipt_id=source["receipt_id"],
            idempotency_key=idempotency_key,
        )

    async def revise_transcript(
        self, transcript_id: str, request: Any, *, idempotency_key: str | None = None
    ) -> Transcript:
        body = parse("TranscriptRevisionCreateRequest", request)
        return await self._service.revise_transcript(
            transcript_id=transcript_id,
            expected_revision=int(body["expected_revision"]),
            text=body["text"],
            source_revision=int(body["source_revision"]) if "source_revision" in body else None,
            idempotency_key=idempotency_key,
        )
