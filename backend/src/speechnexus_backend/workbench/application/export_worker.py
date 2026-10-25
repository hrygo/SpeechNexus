"""Export worker: claim, produce, publish and settle one bounded batch.

The producer is a narrow port because producing content needs the speech pipeline.
Without one the worker is disabled and jobs stay queued: an unconfigured deployment
must never invent an artifact. Cancellation is cooperative — the worker notices a
cancellation at the next reported item, so a started export is not stopped instantly.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Protocol
from uuid import uuid4

from speechnexus_backend.workbench.application.content_writes import ContentWrites
from speechnexus_backend.workbench.domain.exports import (
    ExportArtifact,
    ExportError,
    ExportJob,
    ExportStateConflictError,
)

ReportProgress = Callable[[int], Awaitable[None]]


class ExportCancelledError(Exception):
    """Raised inside a producer when the export was cancelled while producing."""


class ObjectZone(Protocol):
    async def put(self, *, key: str, body: BytesIO, length: int) -> None: ...


class ExportProducer(Protocol):
    async def produce_export(self, job: ExportJob, *, report: ReportProgress) -> ProducedExport: ...


class ExportWorkerStore(Protocol):
    @property
    def content_writes(self) -> ContentWrites: ...

    async def claim_queued_exports(self, *, limit: int) -> list[ExportJob]: ...

    async def record_export_progress(self, *, export_id: str, completed: int) -> ExportJob: ...

    async def complete_export(self, *, export_id: str, artifact: ExportArtifact) -> ExportJob: ...

    async def fail_export(self, *, export_id: str, error: ExportError) -> ExportJob: ...

    async def settle_cancel_export(self, *, export_id: str) -> ExportJob: ...


@dataclass(frozen=True)
class ProducedExport:
    """What a producer hands back: the bundle bytes and the type it really is."""

    content: bytes
    media_type: str


class ExportWorker:
    def __init__(
        self,
        store: ExportWorkerStore,
        zone: ObjectZone | None = None,
        producer: ExportProducer | None = None,
    ) -> None:
        self._store = store
        self._zone = zone
        self._producer = producer

    @property
    def enabled(self) -> bool:
        # Producing needs both a producer and somewhere to publish its bytes.
        return self._producer is not None and self._zone is not None

    async def run_once(self, *, limit: int = 4) -> int:
        """Return how many queued exports this call claimed and settled."""
        if not self.enabled:
            return 0
        assert self._producer is not None and self._zone is not None
        claimed = await self._store.claim_queued_exports(limit=limit)
        for job in claimed:
            try:
                produced = await self._producer.produce_export(job, report=self._reporter(job))
                artifact = await self._publish(job, produced)
            except ExportCancelledError:
                try:
                    # The cancellation was requested while producing; settle it as asked.
                    await self._store.settle_cancel_export(export_id=job.id)
                except ExportStateConflictError:
                    # A producer may only stop for a cancellation that really exists;
                    # aborting a live export is recorded as a failure, not as cancelled.
                    await self._store.fail_export(
                        export_id=job.id,
                        error=ExportError("aborted", "export production was aborted", True),
                    )
                continue
            except Exception:
                # Only a stable, provider-free code reaches callers.
                await self._store.fail_export(
                    export_id=job.id,
                    error=ExportError("unavailable", "export production failed", True),
                )
                continue
            # A completion reply may be lost after commit. Let the store's original
            # outcome remain authoritative instead of recording a compensating failure.
            try:
                await self._store.complete_export(export_id=job.id, artifact=artifact)
            except ExportStateConflictError:
                # PUT is confirmed and the producer has stopped. Only a persisted
                # cancellation may settle here; unknown completion still propagates.
                await self._store.settle_cancel_export(export_id=job.id)
        return len(claimed)

    def _reporter(self, job: ExportJob) -> ReportProgress:
        async def report(completed: int) -> None:
            try:
                await self._store.record_export_progress(export_id=job.id, completed=completed)
            except ExportStateConflictError as error:
                # A cancellation takes the job out of running; stop producing.
                raise ExportCancelledError from error

        return report

    async def _publish(self, job: ExportJob, produced: ProducedExport) -> ExportArtifact:
        assert self._zone is not None
        artifact_id = uuid4().hex
        digest = "sha256:" + hashlib.sha256(produced.content).hexdigest()
        # Validate domain metadata before persisting any content: an empty or
        # malformed producer result is a failed export, never a published object.
        artifact = ExportArtifact(
            id=artifact_id,
            export_id=job.id,
            manifest_revision=job.manifest_revision,
            media_type=produced.media_type,
            bytes=len(produced.content),
            sha256=digest,
            created_at=datetime.now(UTC),
        )
        writes = self._store.content_writes
        write = await writes.reserve_export(export_id=job.id, artifact_id=artifact_id)
        key = f"{write.project_id}/exports/{job.id}/{artifact_id}"
        put_started, put_confirmed = False, False
        try:
            await writes.writing(write, object_key=key, sha256=digest, size=artifact.bytes)
            put_started = True
            await self._zone.put(key=key, body=BytesIO(produced.content), length=artifact.bytes)
            put_confirmed = True
        finally:
            if not put_started or put_confirmed:
                await writes.settle(write)
            else:
                await writes.unknown(write)
        return artifact
