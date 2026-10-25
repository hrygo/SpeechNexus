"""The full production catalog of content owned by a locked workbench project.

Only identities and immutable metadata enter the private plan. The project lock is
the write barrier shared with the owning adapters; no object I/O runs here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from speechnexus_backend.workbench.domain.deletion import DeletionDependency, DeletionTarget
from speechnexus_backend.workbench.domain.saved_assets import project_object_prefix
from speechnexus_backend.workbench.infrastructure.postgres_content_writes import (
    pending_content_write,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import _Session


@dataclass(frozen=True)
class OwnedDeletionSnapshot:
    dependencies: tuple[DeletionDependency, ...]
    content: dict[str, Any]

    @property
    def object_prefixes(self) -> tuple[str, ...]:
        return tuple(self.content["object_prefixes"])


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


class PostgresOwnedDeletionCatalog:
    """Explicit production dependency; historical partial fixtures omit this adapter."""

    async def _rows(self, base: _Session, statement: str, project_id: str) -> list[dict[str, Any]]:
        async with base._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(base._sql(statement), (project_id,))
            return [_json(row) for row in await cursor.fetchall()]

    async def snapshot(self, base: _Session, project_id: str) -> OwnedDeletionSnapshot:
        """Caller must hold the project row lock until acceptance/cleanup commits."""
        books = await self._rows(
            base,
            "SELECT id,casting_revision,status FROM {s}.books WHERE project_id=%s ORDER BY id",
            project_id,
        )
        chapters = await self._rows(
            base,
            "SELECT c.id,c.book_id,c.text_revision,c.ordinal FROM {s}.chapters c "
            "JOIN {s}.books b ON b.id=c.book_id WHERE b.project_id=%s ORDER BY c.id",
            project_id,
        )
        units = await self._rows(
            base,
            "SELECT id,book_id,chapter_id,revision,status,dependencies_sha256,speech_job_id,"
            "saved_asset_id,source_artifact_id,source_receipt_id FROM {s}.render_units "
            "WHERE project_id=%s ORDER BY id",
            project_id,
        )
        generations = await self._rows(
            base,
            "SELECT g.id,g.unit_id,g.job_id,g.dependencies_sha256,g.revision,u.book_id "
            "FROM {s}.render_unit_generations g JOIN {s}.render_units u ON u.id=g.unit_id "
            "WHERE u.project_id=%s ORDER BY g.unit_id,g.revision,g.id",
            project_id,
        )
        exports = await self._rows(
            base,
            "SELECT id,book_id,status,manifest_revision,artifact,progress_completed,"
            "progress_total FROM {s}.export_jobs WHERE project_id=%s ORDER BY id",
            project_id,
        )
        assets = await self._rows(
            base,
            "SELECT id,sha256,object_key,source_artifact_id,source_receipt_id,bytes "
            "FROM {s}.saved_assets WHERE project_id=%s ORDER BY id",
            project_id,
        )
        references = await self._rows(
            base,
            "SELECT u.id,u.project_id,u.revision,u.saved_asset_id FROM {s}.render_units u "
            "JOIN {s}.saved_assets a ON a.id=u.saved_asset_id "
            "WHERE a.project_id=%s AND u.project_id<>a.project_id ORDER BY u.id",
            project_id,
        )
        pending = await pending_content_write(base, project_id)
        targets = {
            row["id"]: DeletionTarget(
                "workbench", "audiobook", row["id"], int(row["casting_revision"])
            )
            for row in books
        }
        asset_targets = {
            row["id"]: DeletionTarget("workbench", "saved_asset", row["id"], 1) for row in assets
        }
        dependencies = [
            DeletionDependency(target, "owned_child", "cascade")
            for target in (*targets.values(), *asset_targets.values())
        ]
        active_books = {row["book_id"] for row in generations}
        active_books.update(row["book_id"] for row in units if row["status"] == "generating")
        active_books.update(
            row["book_id"]
            for row in exports
            if row["status"] in {"queued", "running", "cancelling"}
        )
        dependencies.extend(
            DeletionDependency(targets[book_id], "active_execution", "blocks")
            for book_id in sorted(active_books)
        )
        dependencies.extend(
            DeletionDependency(asset_targets[asset_id], "persistent_reference", "blocks")
            for asset_id in sorted({row["saved_asset_id"] for row in references})
        )
        # A project write has no public content resource yet; the project is the
        # truthful scope of this execution blocker, rather than a fabricated revision.
        if pending:
            project = await base.get_project(project_id)
            if project is None:
                # Cleanup reads the same guard after the project is tombstoned.
                raise ValueError("pending content write prevents cleanup")
            dependencies.append(
                DeletionDependency(
                    DeletionTarget("workbench", "project", project_id, project.metadata_revision),
                    "active_execution",
                    "blocks",
                )
            )
        prefixes = [project_object_prefix(project_id)] if assets or exports else []
        prefixes.extend(f"{row['book_id']}/{row['id']}/" for row in exports)
        content = {
            "project_id": project_id,
            "books": books,
            "chapters": chapters,
            "units": units,
            "generations": generations,
            "exports": exports,
            "assets": assets,
            "references": references,
            "pending_write": pending,
            "object_prefixes": sorted(set(prefixes)),
        }
        return OwnedDeletionSnapshot(tuple(dependencies), content)

    async def stored(
        self, base: _Session, workspace_id: str, plan_id: str
    ) -> dict[str, Any] | None:
        async with base._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                base._sql(
                    "SELECT content_snapshot FROM {s}.deletion_plans "
                    "WHERE workspace_id=%s AND id=%s"
                ),
                (workspace_id, plan_id),
            )
            row = await cursor.fetchone()
        return None if row is None else row["content_snapshot"]
