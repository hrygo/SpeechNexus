"""Export job rows with their book scope, receipts and immutable manifest pin.

Cancelling is idempotent by state: repeating it reports no change instead of
inventing a second cancellation, and a finished export refuses to be cancelled.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import uuid4

from psycopg.types.json import Jsonb

from speechnexus_backend.workbench.application.exports import ExportJobStore
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.exports import (
    ExportArtifact,
    ExportError,
    ExportFormat,
    ExportJob,
    ExportStatus,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)

_COLUMNS = """
    id, project_id, book_id, status, export_format, manifest_revision,
    progress_completed, progress_total, unit_ids, artifact, error, created_at, updated_at
"""


@dataclass(frozen=True)
class _ScopeRow:
    project_id: str
    casting_revision: Decimal
    units: int


@dataclass(frozen=True)
class _ExportRow:
    id: str
    project_id: str
    book_id: str
    status: str
    export_format: str
    manifest_revision: Decimal
    progress_completed: int
    progress_total: int
    unit_ids: list[str] | None
    artifact: dict[str, object] | None
    error: dict[str, object] | None
    created_at: datetime
    updated_at: datetime

    def failure(self) -> ExportError | None:
        if self.error is None:
            return None
        code, message, retryable = (
            self.error.get("code"),
            self.error.get("message"),
            self.error.get("retryable"),
        )
        if (
            not isinstance(code, str)
            or not isinstance(message, str)
            or not isinstance(retryable, bool)
        ):
            raise ValueError("invalid stored export error")
        return ExportError(code, message, retryable)

    def published(self) -> ExportArtifact | None:
        if self.artifact is None:
            return None
        stored = self.artifact
        ident, export_id = stored.get("id"), stored.get("export_id")
        revision, media_type = stored.get("manifest_revision"), stored.get("media_type")
        size, digest, created = (
            stored.get("bytes"),
            stored.get("sha256"),
            stored.get("created_at"),
        )
        if (
            not isinstance(ident, str)
            or not isinstance(export_id, str)
            or not isinstance(revision, int)
            or not isinstance(media_type, str)
            or not isinstance(size, int)
            or not isinstance(digest, str)
            or not isinstance(created, str)
        ):
            raise ValueError("invalid stored export artifact")
        return ExportArtifact(
            id=ident,
            export_id=export_id,
            manifest_revision=revision,
            media_type=media_type,
            bytes=size,
            sha256=digest,
            created_at=datetime.fromisoformat(created),
        )

    def domain(self) -> ExportJob:
        return ExportJob(
            id=self.id,
            book_id=self.book_id,
            status=cast(ExportStatus, self.status),
            format=cast(ExportFormat, self.export_format),
            manifest_revision=int(self.manifest_revision),
            progress_completed=int(self.progress_completed),
            progress_total=int(self.progress_total),
            created_at=self.created_at,
            updated_at=self.updated_at,
            artifact=self.published(),
            error=self.failure(),
        )


@dataclass(frozen=True)
class _ReceiptRow:
    intent_digest: str
    export_id: str


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _artifact_json(value: ExportArtifact) -> dict[str, object]:
    return {
        "id": value.id,
        "export_id": value.export_id,
        "manifest_revision": value.manifest_revision,
        "media_type": value.media_type,
        "bytes": value.bytes,
        "sha256": value.sha256,
        "created_at": value.created_at.isoformat(),
    }


def _error_json(value: ExportError) -> dict[str, object]:
    return {"code": value.code, "message": value.message, "retryable": value.retryable}


class PostgresExports(ExportJobStore):
    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._versions = versions

    async def _scope(self, base: _Session, book_id: str) -> _ScopeRow | None:
        return await base._one(
            _ScopeRow,
            """
            SELECT b.project_id, b.casting_revision,
                (SELECT count(*) FROM {s}.render_units u WHERE u.book_id = b.id) AS units
            FROM {s}.books b WHERE b.id = %s
        """,
            (book_id,),
        )

    async def _project(
        self, base: _Session, project_id: str, workspace_id: str, *, lock: bool
    ) -> None:
        project = await base.get_project(project_id, for_update=lock)
        if project is None or project.workspace_id != workspace_id:
            raise ResourceNotFoundError

    async def _row(
        self, base: _Session, export_id: str, *, lock: bool = False
    ) -> _ExportRow | None:
        suffix = " FOR UPDATE" if lock else ""
        return await base._one(
            _ExportRow,
            f"SELECT {_COLUMNS} FROM {{s}}.export_jobs WHERE id = %s{suffix}",
            (export_id,),
        )

    async def _receipt(self, base: _Session, scope_id: str, key_digest: str) -> _ReceiptRow | None:
        return await base._one(
            _ReceiptRow,
            """
            SELECT intent_digest, export_id FROM {s}.export_job_receipts
            WHERE scope_id = %s AND key_digest = %s
        """,
            (scope_id, key_digest),
        )

    async def _selected(self, base: _Session, book_id: str, unit_ids: list[str]) -> int:
        row = await base._one(
            _ScopeRow,
            """
            SELECT b.project_id, b.casting_revision,
                (SELECT count(*) FROM {s}.render_units u
                    WHERE u.book_id = b.id AND u.id = ANY(%s::text[])) AS units
            FROM {s}.books b WHERE b.id = %s
        """,
            (list(unit_ids), book_id),
        )
        if row is None or int(row.units) != len(unit_ids):
            # An export may only name units of the book it pins.
            raise ResourceNotFoundError
        return len(unit_ids)

    async def create_export(
        self,
        *,
        workspace_id: str,
        book_id: str,
        export_format: str,
        expected_casting_revision: int,
        unit_ids: list[str] | None,
        key: str | None,
    ) -> ExportJob:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            scope = await self._scope(base, book_id)
            if scope is None:
                raise ResourceNotFoundError
            await self._project(base, scope.project_id, workspace_id, lock=True)
            if int(scope.casting_revision) != expected_casting_revision:
                # The caller asked to export a manifest that is no longer current.
                raise RevisionConflictError(expected_casting_revision, int(scope.casting_revision))
            digest, intent = (
                (None, "")
                if key is None
                else (
                    hashlib.sha256(key.encode("utf-8")).hexdigest(),
                    _digest(
                        [
                            "export.create",
                            scope.project_id,
                            book_id,
                            export_format,
                            expected_casting_revision,
                            None if unit_ids is None else sorted(unit_ids),
                        ]
                    ),
                )
            )
            if digest is not None:
                stored = await self._receipt(base, scope.project_id, digest)
                if stored is not None:
                    if stored.intent_digest != intent:
                        raise IdempotencyConflictError
                    previous = await self._row(base, stored.export_id)
                    if previous is None:
                        raise ResourceNotFoundError
                    return previous.domain()
            if unit_ids is None:
                total = int(scope.units)
                if total < 1:
                    raise InvalidVersionChangeError("empty workbench export")
                selection: list[str] | None = None
            else:
                total = await self._selected(base, book_id, unit_ids)
                selection = list(unit_ids)
            now = await base.now()
            export = ExportJob.create(
                export_id=uuid4().hex,
                book_id=book_id,
                export_format=export_format,
                manifest_revision=int(scope.casting_revision),
                progress_total=total,
                now=now,
            )
            await base._execute(
                """
                INSERT INTO {s}.export_jobs (
                    id, project_id, book_id, status, export_format, manifest_revision,
                    progress_completed, progress_total, unit_ids, artifact, error,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s)
            """,
                (
                    export.id,
                    scope.project_id,
                    book_id,
                    export.status,
                    export.format,
                    export.manifest_revision,
                    export.progress_completed,
                    export.progress_total,
                    None if selection is None else Jsonb(selection),
                    now,
                    now,
                ),
            )
            if digest is not None:
                await base._execute(
                    """
                    INSERT INTO {s}.export_job_receipts
                        (scope_id, key_digest, intent_digest, export_id, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                """,
                    (scope.project_id, digest, intent, export.id, now),
                )
            return export

    async def get_export(self, *, export_id: str, workspace_id: str) -> ExportJob:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._row(base, export_id)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=False)
            return row.domain()

    async def cancel_export(self, *, export_id: str, workspace_id: str) -> tuple[ExportJob, bool]:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._row(base, export_id, lock=True)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=True)
            current = row.domain()
            if current.status in {"cancelling", "cancelled"}:
                return current, False
            # A finished export refuses cancellation: the domain owns that rule.
            cancelled = current.request_cancel(now=await base.now())
            await base._execute(
                """
                UPDATE {s}.export_jobs SET status = %s, updated_at = %s WHERE id = %s
            """,
                (cancelled.status, cancelled.updated_at, export_id),
            )
            return cancelled, True

    async def _advance(
        self, export_id: str, move: Callable[[ExportJob, datetime], ExportJob]
    ) -> ExportJob:
        """Apply one domain transition to the locked row and persist its columns."""
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._row(base, export_id, lock=True)
            if row is None:
                raise ResourceNotFoundError
            advanced = move(row.domain(), await base.now())
            await base._execute(
                """
                UPDATE {s}.export_jobs SET status = %s, progress_completed = %s,
                    progress_total = %s, artifact = %s, error = %s, updated_at = %s
                WHERE id = %s
            """,
                (
                    advanced.status,
                    advanced.progress_completed,
                    advanced.progress_total,
                    None if advanced.artifact is None else Jsonb(_artifact_json(advanced.artifact)),
                    None if advanced.error is None else Jsonb(_error_json(advanced.error)),
                    advanced.updated_at,
                    export_id,
                ),
            )
            return advanced

    async def start_export(self, *, export_id: str) -> ExportJob:
        return await self._advance(export_id, lambda job, now: job.start(now=now))

    async def record_export_progress(self, *, export_id: str, completed: int) -> ExportJob:
        return await self._advance(
            export_id, lambda job, now: job.record_progress(completed=completed, now=now)
        )

    async def complete_export(self, *, export_id: str, artifact: ExportArtifact) -> ExportJob:
        return await self._advance(
            export_id, lambda job, now: job.complete(artifact=artifact, now=now)
        )

    async def fail_export(self, *, export_id: str, error: ExportError) -> ExportJob:
        return await self._advance(export_id, lambda job, now: job.fail(error=error, now=now))

    async def expire_export(self, *, export_id: str) -> ExportJob:
        return await self._advance(export_id, lambda job, now: job.expire(now=now))

    async def claim_queued_exports(self, *, limit: int) -> list[ExportJob]:
        """Claim up to `limit` queued exports for this worker, marking them running."""
        if type(limit) is not int or limit < 1:
            raise InvalidVersionChangeError("invalid workbench export claim")
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            rows = await base._many(
                _ExportRow,
                f"""
                SELECT {_COLUMNS} FROM {{s}}.export_jobs
                WHERE status = 'queued' ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED LIMIT %s
            """,
                (limit,),
            )
            claimed: list[ExportJob] = []
            for row in rows or []:
                running = row.domain().start(now=await base.now())
                await base._execute(
                    """
                    UPDATE {s}.export_jobs SET status = %s, updated_at = %s WHERE id = %s
                """,
                    (running.status, running.updated_at, running.id),
                )
                claimed.append(running)
            return claimed

    async def settle_cancel_export(self, *, export_id: str) -> ExportJob:
        return await self._advance(export_id, lambda job, now: job.settle_cancel(now=now))
