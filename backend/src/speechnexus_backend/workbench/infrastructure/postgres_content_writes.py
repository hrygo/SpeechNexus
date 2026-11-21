"""Short transactions fence each external object write before deletion can be accepted."""

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from psycopg.types.json import Jsonb

from speechnexus_backend.workbench.application.content_writes import ContentWrite
from speechnexus_backend.workbench.application.export_worker import ExportCancelledError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    VersionStoreError,
    _Session,
)


@dataclass(frozen=True)
class _Pending:
    present: bool


async def pending_content_write(base: _Session, project_id: str) -> bool:
    row = await base._one(
        _Pending,
        "SELECT EXISTS(SELECT 1 FROM {s}.content_write_intents "
        "WHERE project_id=%s AND state <> 'settled') AS present",
        (project_id,),
    )
    assert row is not None
    return row.present


@dataclass(frozen=True)
class _Write:
    id: str
    resource_id: str
    intent_digest: str
    state: str


@dataclass(frozen=True)
class _Export:
    project_id: str
    status: str


@dataclass(frozen=True)
class _Changed:
    id: str


class PostgresContentWrites:
    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._versions = versions

    async def reserve_asset(
        self,
        *,
        project_id: str,
        workspace_id: str,
        asset_id: str,
        source_artifact_id: str,
        source_receipt_id: str,
        display_name: str | None,
        key: str | None,
        intent_digest: str,
    ) -> ContentWrite:
        intent: dict[str, object] = {
            "workspace_id": workspace_id,
            "source_artifact_id": source_artifact_id,
            "source_receipt_id": source_receipt_id,
            "display_name": display_name,
        }
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            project = await base.get_project(project_id, for_update=True)
            if project is None or project.workspace_id != workspace_id:
                raise ResourceNotFoundError
            return await self._reserve(
                base, project_id, "saved_asset", asset_id, key, intent_digest, intent
            )

    async def reserve_export(self, *, export_id: str, artifact_id: str) -> ContentWrite:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await base._one(
                _Export, "SELECT project_id,status FROM {s}.export_jobs WHERE id=%s", (export_id,)
            )
            if row is None or await base.get_project(row.project_id, for_update=True) is None:
                raise ResourceNotFoundError
            row = await base._one(
                _Export,
                "SELECT project_id,status FROM {s}.export_jobs WHERE id=%s FOR UPDATE",
                (export_id,),
            )
            if row is None:
                raise ResourceNotFoundError
            if row.status == "cancelling":
                raise ExportCancelledError
            if row.status != "running":
                raise VersionStoreError(sqlstate=None, commit_unknown=True)
            intent: dict[str, object] = {"export_id": export_id, "artifact_id": artifact_id}
            digest = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
            return await self._reserve(
                base, row.project_id, "book_export", artifact_id, export_id, digest, intent
            )

    async def _reserve(
        self,
        base: _Session,
        project_id: str,
        kind: str,
        resource_id: str,
        key: str | None,
        digest: str,
        intent: dict[str, object],
    ) -> ContentWrite:
        key_digest = hashlib.sha256(key.encode()).hexdigest() if key is not None else None
        previous = (
            await base._one(
                _Write,
                "SELECT id,resource_id,intent_digest,state FROM {s}.content_write_intents "
                "WHERE project_id=%s AND resource_kind=%s AND key_digest=%s",
                (project_id, kind, key_digest),
            )
            if key_digest is not None
            else None
        )
        token = uuid4().hex
        now = await base.now()
        if previous is not None:
            if previous.intent_digest != digest:
                raise IdempotencyConflictError
            # A reserved owner cannot send PUT until its token wins the writing CAS.
            # Writing/unknown owners must never be stolen based on time or a retry.
            if previous.state in {"writing", "unknown"}:
                raise VersionStoreError(sqlstate=None, commit_unknown=True)
            await base._execute(
                "UPDATE {s}.content_write_intents SET token=%s,intent=%s,state='reserved',"
                "object_key=NULL,sha256=NULL,bytes=NULL,updated_at=%s WHERE id=%s",
                (token, Jsonb(intent), now, previous.id),
            )
            return ContentWrite(previous.id, project_id, previous.resource_id, token)
        identity = uuid4().hex
        await base._execute(
            "INSERT INTO {s}.content_write_intents "
            "(id,project_id,resource_kind,resource_id,token,key_digest,intent_digest,intent,"
            "state,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'reserved',%s,%s)",
            (
                identity,
                project_id,
                kind,
                resource_id,
                token,
                key_digest,
                digest,
                Jsonb(intent),
                now,
                now,
            ),
        )
        return ContentWrite(identity, project_id, resource_id, token)

    async def writing(
        self, write: ContentWrite, *, object_key: str, sha256: str, size: int
    ) -> None:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            if await base.get_project(write.project_id, for_update=True) is None:
                raise ResourceNotFoundError
            changed = await base._one(
                _Changed,
                "UPDATE {s}.content_write_intents SET state='writing',object_key=%s,sha256=%s,"
                "bytes=%s,updated_at=%s WHERE id=%s AND token=%s AND state='reserved' RETURNING id",
                (object_key, sha256, size, await base.now(), write.id, write.token),
            )
            if changed is None:
                raise VersionStoreError(sqlstate=None, commit_unknown=True)

    async def settle(self, write: ContentWrite) -> None:
        await self._finish(write, unknown=False)

    async def unknown(self, write: ContentWrite) -> None:
        await self._finish(write, unknown=True)

    async def _finish(self, write: ContentWrite, *, unknown: bool) -> None:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            await base._execute(
                "UPDATE {s}.content_write_intents SET state=%s,"
                "intent=CASE WHEN %s THEN intent ELSE NULL END,updated_at=%s "
                "WHERE id=%s AND token=%s AND state IN ('reserved','writing')",
                (
                    "unknown" if unknown else "settled",
                    unknown,
                    await base.now(),
                    write.id,
                    write.token,
                ),
            )
