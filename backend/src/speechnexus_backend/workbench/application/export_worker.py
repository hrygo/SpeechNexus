"""Export worker: claim, produce, publish and settle one bounded batch.

The producer assembles a fixed selection of saved audio; it must not invoke inference.
Without a producer and validator the worker is disabled and jobs stay queued:
an unconfigured deployment must never invent an artifact. Cancellation is cooperative:
the worker notices it at the next progress check, so an export is not stopped instantly.
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


class ExportValidator(Protocol):
    async def validate_export(self, job: ExportJob, produced: ProducedExport) -> None: ...


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
        validator: ExportValidator | None = None,
    ) -> None:
        self._store = store
        self._zone = zone
        self._producer = producer
        self._validator = validator

    @property
    def enabled(self) -> bool:
        return self._producer is not None and self._zone is not None and self._validator is not None

    async def run_once(self, *, limit: int = 4) -> int:
        """Return how many queued exports this call claimed and settled."""
        if not self.enabled:
            return 0
        assert self._producer is not None and self._zone is not None and self._validator is not None
        claimed = await self._store.claim_queued_exports(limit=limit)
        for job in claimed:
            try:
                progress, produced = await self._produce(job)
                await self._validator.validate_export(progress, produced)
                # Re-acknowledge only the already persisted count. This checks a
                # cancellation accepted while decoding before reserving any write.
                progress = await self._record_progress(progress, progress.progress_completed)
                artifact = await self._publish(progress, produced)
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
                try:
                    await self._store.fail_export(
                        export_id=job.id,
                        error=ExportError("unavailable", "export production failed", True),
                    )
                except ExportStateConflictError:
                    # A persisted cancellation can win while validation fails.
                    # Production/validation has stopped, so settle that request.
                    # Unknown failure commits still propagate without compensation.
                    await self._store.settle_cancel_export(export_id=job.id)
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

    async def _produce(self, job: ExportJob) -> tuple[ExportJob, ProducedExport]:
        assert self._producer is not None
        progress, reporting = job, True

        async def report(completed: int) -> None:
            nonlocal progress
            if not reporting:
                raise ValueError("export producer has finished reporting")
            progress = await self._record_progress(job, completed)

        try:
            produced = await self._producer.produce_export(job, report=report)
        finally:
            reporting = False
        if progress.progress_completed != job.progress_total:
            raise ValueError("export production is incomplete")
        return progress, produced

    async def _record_progress(self, job: ExportJob, completed: int) -> ExportJob:
        try:
            current = await self._store.record_export_progress(
                export_id=job.id, completed=completed
            )
        except ExportStateConflictError as error:
            raise ExportCancelledError from error
        if (
            current.id != job.id
            or current.book_id != job.book_id
            or current.manifest_revision != job.manifest_revision
            or current.format != job.format
            or current.progress_total != job.progress_total
            or current.status != "running"
            or current.progress_completed != completed
        ):
            raise ValueError("export progress acknowledgement does not match its intent")
        return current

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
