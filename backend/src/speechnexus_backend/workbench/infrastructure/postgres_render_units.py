"""Render-unit rows with their chapter scope, receipts and dispatch history.

The unit pins what a narration segment was derived from, so every write recomputes the
dependency digest from stored state instead of trusting a caller-supplied digest. The
chapter decides its owning project, and that project row is the serialization point.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import uuid4

from speechnexus_backend.workbench.application.listings import ListingPosition
from speechnexus_backend.workbench.application.render_units import RenderUnitStore
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.render_units import (
    RenderDependencies,
    RenderGeneration,
    RenderSource,
    RenderStatus,
    RenderUnit,
    RenderUnitSummary,
    RevisionPin,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)

# C's public casting model narrates a book with one role; recasting is versioned on the
# book, so the role identity is stable while the voice it pins changes revisions.
NARRATOR_ROLE = "narrator"

_UNIT_COLUMNS = """
    id, project_id, book_id, chapter_id, ordinal, revision, status, text,
    voice_id, voice_revision, model, model_binding_id, model_binding_revision, role_id,
    text_plan_revision, original_revision, working_revision,
    pronunciation_id, pronunciation_revision, profile_id, profile_revision,
    dependencies_sha256, speech_job_id, saved_asset_id, source_artifact_id, source_receipt_id,
    created_at, updated_at
"""


@dataclass(frozen=True)
class _UnitRow:
    id: str
    project_id: str
    book_id: str
    chapter_id: str
    ordinal: Decimal
    revision: Decimal
    status: str
    text: str
    voice_id: str
    voice_revision: Decimal
    model: str
    model_binding_id: str
    model_binding_revision: Decimal
    role_id: str
    text_plan_revision: Decimal
    original_revision: Decimal
    working_revision: Decimal
    pronunciation_id: str | None
    pronunciation_revision: Decimal | None
    profile_id: str | None
    profile_revision: Decimal | None
    dependencies_sha256: str
    speech_job_id: str | None
    saved_asset_id: str | None
    source_artifact_id: str | None
    source_receipt_id: str | None
    created_at: datetime
    updated_at: datetime

    def dependencies(self) -> RenderDependencies:
        def optional(identifier: str | None, revision: Decimal | None) -> RevisionPin | None:
            if identifier is None or revision is None:
                return None
            return RevisionPin(identifier, int(revision))

        return RenderDependencies(
            original=RevisionPin(self.chapter_id, int(self.original_revision)),
            working=RevisionPin(self.chapter_id, int(self.working_revision)),
            chapter_id=self.chapter_id,
            role_id=self.role_id,
            text_plan=RevisionPin(self.chapter_id, int(self.text_plan_revision)),
            voice=RevisionPin(self.voice_id, int(self.voice_revision)),
            model=self.model,
            model_binding=RevisionPin(self.model_binding_id, int(self.model_binding_revision)),
            text=self.text,
            pronunciation=optional(self.pronunciation_id, self.pronunciation_revision),
            profile=optional(self.profile_id, self.profile_revision),
        )

    def unit(
        self,
        generations: tuple[RenderGeneration, ...],
        *,
        saved_asset_id: str | None = None,
    ) -> RenderUnit:
        pinned = self.dependencies()
        source = None
        if self.speech_job_id is not None:
            # A result is fixed by its producer identity, not by a workbench-side copy.
            assert self.source_artifact_id is not None
            assert self.source_receipt_id is not None
            source = RenderSource(
                unit_id=self.id,
                job_id=self.speech_job_id,
                artifact_id=self.source_artifact_id,
                receipt_id=self.source_receipt_id,
                dependencies_sha256=self.dependencies_sha256,
                saved_asset_id=saved_asset_id,
            )
        unit = RenderUnit(
            id=self.id,
            book_id=self.book_id,
            ordinal=int(self.ordinal),
            revision=int(self.revision),
            dependencies=pinned,
            status=cast(RenderStatus, self.status),
            updated_at=self.updated_at,
            generations=generations,
            source=source,
        )
        assert unit.dependencies.sha256 == self.dependencies_sha256
        return unit


@dataclass(frozen=True)
class _GenerationRow:
    id: str
    job_id: str
    dependencies_sha256: str
    revision: Decimal

    def domain(self) -> RenderGeneration:
        return RenderGeneration(self.id, self.job_id, self.dependencies_sha256, int(self.revision))


@dataclass(frozen=True)
class _ReceiptRow:
    intent_digest: str
    unit_id: str


@dataclass(frozen=True)
class _GeneratingRow:
    id: str


class _UnitWithCopyRow(_UnitRow):
    # Newest workbench-owned copy of this unit's producer result, if one exists yet.
    saved_copy_id: str | None


@dataclass(frozen=True)
class _UnitListingRow(_UnitWithCopyRow):
    # The chapter that owns the listed unit, so one page row is self-describing.
    parent_id: str

    def summary(self, generations: tuple[RenderGeneration, ...]) -> RenderUnitSummary:
        return RenderUnitSummary(
            id=self.id,
            created_at=self.created_at,
            unit=self.unit(generations, saved_asset_id=self.saved_copy_id),
        )


@dataclass(frozen=True)
class _UnitGenerationRow(_GenerationRow):
    unit_id: str


@dataclass(frozen=True)
class _ScopeRow:
    id: str
    book_id: str
    project_id: str
    text_revision: Decimal


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class PostgresRenderUnits(RenderUnitStore):
    """A create derives its pins from the chapter row; no caller digest is trusted."""

    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._versions = versions

    async def _project(
        self, base: _Session, project_id: str, workspace_id: str, *, lock: bool
    ) -> None:
        project = await base.get_project(project_id, for_update=lock)
        if project is None or project.workspace_id != workspace_id:
            raise ResourceNotFoundError

    async def _unit_row(self, base: _Session, unit_id: str) -> _UnitWithCopyRow | None:
        return await base._one(
            _UnitWithCopyRow,
            """
            SELECT u.*, sa.id AS saved_copy_id
            FROM {s}.render_units u
            LEFT JOIN LATERAL (
                SELECT a.id FROM {s}.saved_assets a
                WHERE a.project_id = u.project_id
                    AND a.source_artifact_id = u.source_artifact_id
                    AND a.source_receipt_id = u.source_receipt_id
                ORDER BY a.created_at DESC, a.id DESC LIMIT 1
            ) sa ON true
            WHERE u.id = %s
        """,
            (unit_id,),
        )

    async def _generations(self, base: _Session, unit_id: str) -> tuple[RenderGeneration, ...]:
        rows = await base._many(
            _GenerationRow,
            """
            SELECT id, job_id, dependencies_sha256, revision
            FROM {s}.render_unit_generations WHERE unit_id = %s ORDER BY revision
        """,
            (unit_id,),
        )
        return tuple(row.domain() for row in rows)

    async def list_units(
        self, *, chapter_id: str, workspace_id: str, limit: int, after: ListingPosition | None
    ) -> tuple[RenderUnitSummary, ...] | None:
        # One statement resolves chapter visibility and its page; the generations of
        # that page follow in a second statement, so no unit is read on its own.
        seek = "" if after is None else " AND (u.created_at, u.id) < (%s, %s)"
        params: list[object] = []
        if after is not None:
            params.extend((after.created_at, after.item_id))
        params.extend((limit, chapter_id, workspace_id))
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            rows = await base._many(
                _UnitListingRow,
                """
                SELECT c.id AS parent_id, u.*, sa.id AS saved_copy_id
                FROM {s}.chapters c
                JOIN {s}.books b ON b.id = c.book_id
                JOIN {s}.projects p ON p.id = b.project_id
                LEFT JOIN LATERAL (
                    SELECT """
                + _UNIT_COLUMNS
                + """
                    FROM {s}.render_units u
                    WHERE u.chapter_id = c.id
                """
                + seek
                + """
                    ORDER BY u.created_at DESC, u.id DESC LIMIT %s
                ) u ON true
                LEFT JOIN LATERAL (
                    SELECT a.id FROM {s}.saved_assets a
                    WHERE a.project_id = u.project_id
                        AND a.source_artifact_id = u.source_artifact_id
                        AND a.source_receipt_id = u.source_receipt_id
                    ORDER BY a.created_at DESC, a.id DESC LIMIT 1
                ) sa ON true
                WHERE c.id = %s AND p.workspace_id = %s AND NOT EXISTS (
                    SELECT 1 FROM {s}.resource_tombstones d
                    WHERE d.resource_kind = 'project' AND d.resource_id = p.id
                    UNION ALL
                    SELECT 1 FROM {s}.deletion_ledger l, jsonb_array_elements(l.deleted) e
                    WHERE e->>1 = 'project' AND e->>2 = p.id)
                """,
                tuple(params),
            )
            if not rows:
                return None
            generations = await self._page_generations(base, tuple(row.id for row in rows))
        return tuple(row.summary(generations.get(row.id, ())) for row in rows if row.id is not None)

    async def _page_generations(
        self, base: _Session, unit_ids: tuple[str, ...]
    ) -> dict[str, tuple[RenderGeneration, ...]]:
        rows = await base._many(
            _UnitGenerationRow,
            """
            SELECT unit_id, id, job_id, dependencies_sha256, revision
            FROM {s}.render_unit_generations WHERE unit_id = ANY(%s)
            ORDER BY unit_id, revision
        """,
            (list(unit_ids),),
        )
        grouped: dict[str, list[RenderGeneration]] = {}
        for row in rows:
            grouped.setdefault(row.unit_id, []).append(row.domain())
        return {unit_id: tuple(items) for unit_id, items in grouped.items()}

    async def _scope(self, base: _Session, chapter_id: str) -> _ScopeRow | None:
        return await base._one(
            _ScopeRow,
            """
            SELECT c.id, c.book_id, b.project_id, c.text_revision FROM {s}.chapters c
            JOIN {s}.books b ON b.id = c.book_id WHERE c.id = %s
        """,
            (chapter_id,),
        )

    async def _receipt(self, base: _Session, scope_id: str, key_digest: str) -> _ReceiptRow | None:
        return await base._one(
            _ReceiptRow,
            """
            SELECT intent_digest, unit_id FROM {s}.render_unit_receipts
            WHERE scope_id = %s AND key_digest = %s
        """,
            (scope_id, key_digest),
        )

    @staticmethod
    def _notes(key: str | None, intent: list[object]) -> tuple[str | None, str]:
        if key is None:
            return None, ""
        return hashlib.sha256(key.encode("utf-8")).hexdigest(), _digest(intent)

    async def create_unit(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        ordinal: int,
        text: str,
        voice_id: str,
        voice_revision: int,
        model: str,
        model_binding_id: str,
        model_binding_revision: int,
        key: str | None,
    ) -> RenderUnit:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            scope = await self._scope(base, chapter_id)
            if scope is None:
                raise ResourceNotFoundError
            await self._project(base, scope.project_id, workspace_id, lock=True)
            digest, intent = self._notes(
                key,
                [
                    "render.create",
                    chapter_id,
                    ordinal,
                    text,
                    voice_id,
                    voice_revision,
                    model,
                    model_binding_id,
                    model_binding_revision,
                ],
            )
            if digest is not None:
                stored = await self._receipt(base, chapter_id, digest)
                if stored is not None:
                    if stored.intent_digest != intent:
                        raise IdempotencyConflictError
                    # A receipt cannot outlive its row, so its absence is resource absence.
                    previous = await self._unit_row(base, stored.unit_id)
                    if previous is None:
                        raise ResourceNotFoundError
                    return previous.unit(await self._generations(base, previous.id))
            pin = int(scope.text_revision)
            now = await base.now()
            dependencies = RenderDependencies(
                original=RevisionPin(chapter_id, pin),
                working=RevisionPin(chapter_id, pin),
                chapter_id=chapter_id,
                role_id=NARRATOR_ROLE,
                text_plan=RevisionPin(chapter_id, pin),
                voice=RevisionPin(voice_id, voice_revision),
                model=model,
                model_binding=RevisionPin(model_binding_id, model_binding_revision),
                text=text,
            )
            unit = RenderUnit.create(
                unit_id=uuid4().hex,
                book_id=scope.book_id,
                ordinal=ordinal,
                dependencies=dependencies,
                now=now,
            )
            await self._insert(base, unit, scope.project_id, now)
            if digest is not None:
                await base._execute(
                    """
                    INSERT INTO {s}.render_unit_receipts
                        (scope_id, key_digest, intent_digest, unit_id, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                """,
                    (chapter_id, digest, intent, unit.id, now),
                )
            return unit

    async def _insert(
        self, base: _Session, unit: RenderUnit, project_id: str, now: datetime
    ) -> None:
        dependencies = unit.dependencies
        await base._execute(
            """
            INSERT INTO {s}.render_units (
                id, project_id, book_id, chapter_id, ordinal, revision, status, text,
                voice_id, voice_revision, model, model_binding_id, model_binding_revision,
                role_id, text_plan_revision, original_revision, working_revision,
                dependencies_sha256, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """,
            (
                unit.id,
                project_id,
                unit.book_id,
                dependencies.chapter_id,
                unit.ordinal,
                unit.revision,
                unit.status,
                dependencies.text,
                dependencies.voice.id,
                dependencies.voice.revision,
                dependencies.model,
                dependencies.model_binding.id,
                dependencies.model_binding.revision,
                dependencies.role_id,
                dependencies.text_plan.revision,
                dependencies.original.revision,
                dependencies.working.revision,
                dependencies.sha256,
                now,
                now,
            ),
        )

    async def get_unit(self, *, unit_id: str, workspace_id: str) -> RenderUnit:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._unit_row(base, unit_id)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=False)
            return row.unit(
                await self._generations(base, unit_id), saved_asset_id=row.saved_copy_id
            )

    async def start_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        expected_revision: int,
        job_id: str,
        generation_id: str,
    ) -> RenderUnit:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._unit_row(base, unit_id)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=True)
            current = row.unit(
                await self._generations(base, unit_id), saved_asset_id=row.saved_copy_id
            )
            now = await base.now()
            started = current.start(
                expected_revision=expected_revision,
                job_id=job_id,
                generation_id=generation_id,
                now=now,
            )
            generation = started.generations[-1]
            await base._execute(
                """
                UPDATE {s}.render_units SET revision = %s, status = %s, updated_at = %s
                WHERE id = %s
            """,
                (started.revision, started.status, started.updated_at, unit_id),
            )
            await base._execute(
                """
                INSERT INTO {s}.render_unit_generations
                    (id, unit_id, job_id, dependencies_sha256, revision, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """,
                (
                    generation.id,
                    unit_id,
                    generation.job_id,
                    generation.dependencies_sha256,
                    generation.revision,
                    now,
                ),
            )
            return started

    async def complete_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        generation_id: str,
        source: RenderSource,
    ) -> RenderUnit:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._unit_row(base, unit_id)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=True)
            current = row.unit(
                await self._generations(base, unit_id), saved_asset_id=row.saved_copy_id
            )
            now = await base.now()
            completed = current.complete(generation_id=generation_id, source=source, now=now)
            await base._execute(
                """
                UPDATE {s}.render_units SET
                    revision = %s, status = %s, source_artifact_id = %s,
                    source_receipt_id = %s, updated_at = %s
                WHERE id = %s
            """,
                (
                    completed.revision,
                    completed.status,
                    source.artifact_id,
                    source.receipt_id,
                    completed.updated_at,
                    unit_id,
                ),
            )
            return completed

    async def fail_unit(self, *, unit_id: str, workspace_id: str, generation_id: str) -> RenderUnit:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._unit_row(base, unit_id)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=True)
            current = row.unit(
                await self._generations(base, unit_id), saved_asset_id=row.saved_copy_id
            )
            now = await base.now()
            failed = current.fail(generation_id=generation_id, now=now)
            await base._execute(
                """
                UPDATE {s}.render_units SET
                    revision = %s, status = %s, source_artifact_id = NULL,
                    source_receipt_id = NULL, updated_at = %s
                WHERE id = %s
            """,
                (failed.revision, failed.status, failed.updated_at, unit_id),
            )
            return failed

    async def scan_generating(self, *, workspace_id: str, limit: int) -> tuple[str, ...]:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            rows = await base._many(
                _GeneratingRow,
                """
                SELECT u.id FROM {s}.render_units u
                JOIN {s}.projects p ON p.id = u.project_id
                WHERE u.status = 'generating' AND p.workspace_id = %s
                    AND NOT EXISTS (
                        SELECT 1 FROM {s}.resource_tombstones d
                        WHERE d.resource_kind = 'project' AND d.resource_id = p.id
                        UNION ALL
                        SELECT 1 FROM {s}.deletion_ledger l,
                            jsonb_array_elements(l.deleted) e
                        WHERE e->>1 = 'project' AND e->>2 = p.id)
                ORDER BY u.updated_at, u.id LIMIT %s
            """,
                (workspace_id, limit),
            )
        return tuple(row.id for row in rows)

    async def update_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        expected_revision: int,
        text: str | None = None,
        voice_id: str | None = None,
        voice_revision: int | None = None,
        model: str | None = None,
        model_binding_id: str | None = None,
        model_binding_revision: int | None = None,
    ) -> RenderUnit:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._unit_row(base, unit_id)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=True)
            current = row.unit(
                await self._generations(base, unit_id), saved_asset_id=row.saved_copy_id
            )
            stored = row.dependencies()
            dependencies = RenderDependencies(
                original=stored.original,
                working=stored.working,
                chapter_id=row.chapter_id,
                role_id=row.role_id,
                text_plan=stored.text_plan,
                voice=RevisionPin(
                    voice_id if voice_id is not None else row.voice_id,
                    voice_revision if voice_revision is not None else int(row.voice_revision),
                ),
                model=model if model is not None else row.model,
                model_binding=RevisionPin(
                    model_binding_id if model_binding_id is not None else row.model_binding_id,
                    model_binding_revision
                    if model_binding_revision is not None
                    else int(row.model_binding_revision),
                ),
                text=text if text is not None else row.text,
                pronunciation=stored.pronunciation,
                profile=stored.profile,
            )
            now = await base.now()
            revised = current.revise(
                expected_revision=expected_revision, dependencies=dependencies, now=now
            )
            if revised.revision != current.revision:
                await base._execute(
                    """
                    UPDATE {s}.render_units SET
                        revision = %s, status = %s, text = %s, voice_id = %s, voice_revision = %s,
                        model = %s, model_binding_id = %s, model_binding_revision = %s,
                        dependencies_sha256 = %s, updated_at = %s
                    WHERE id = %s
                """,
                    (
                        revised.revision,
                        revised.status,
                        dependencies.text,
                        dependencies.voice.id,
                        dependencies.voice.revision,
                        dependencies.model,
                        dependencies.model_binding.id,
                        dependencies.model_binding.revision,
                        dependencies.sha256,
                        revised.updated_at,
                        unit_id,
                    ),
                )
            return revised
