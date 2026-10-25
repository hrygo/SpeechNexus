"""Workbench deletion plans/intent on the same bounded version transaction adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from speechnexus_backend.workbench.application.deletion_ports import (
    DeletionFamily,
    DeletionReceipt,
    DeletionSession,
)
from speechnexus_backend.workbench.domain.deletion import (
    DeletionDependency,
    DeletionMode,
    DeletionPlan,
    DeletionRecord,
    DeletionResult,
    DeletionTarget,
    DependencyDisposition,
    DependencyRelation,
    ResourceKind,
    ResourceOwner,
    dependency_snapshot,
)
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)


@dataclass(frozen=True)
class _Identity:
    id: str
    head_revision: Decimal


@dataclass(frozen=True)
class _Parent:
    project_id: str


@dataclass(frozen=True)
class _MeetingDependency:
    id: str
    workspace_id: str
    version: Decimal


@dataclass(frozen=True)
class _PlanRow:
    id: str
    workspace_id: str
    target_kind: ResourceKind
    target_id: str
    target_revision: Decimal
    mode: DeletionMode
    created_at: datetime
    expires_at: datetime
    dependencies: list[list[str]]
    plan_digest: str

    def domain(self) -> DeletionPlan:
        dependencies = tuple(
            DeletionDependency(
                DeletionTarget(
                    cast(ResourceOwner, owner), cast(ResourceKind, kind), identity, int(rev)
                ),
                cast(DependencyRelation, relation),
                cast(DependencyDisposition, disposition),
            )
            for owner, kind, identity, rev, relation, disposition in self.dependencies
        )
        plan = DeletionPlan(
            self.id,
            self.workspace_id,
            DeletionTarget(
                "workbench", self.target_kind, self.target_id, int(self.target_revision)
            ),
            self.mode,
            1,
            self.created_at,
            self.expires_at,
            dependencies,
        )
        if plan_digest(plan) != self.plan_digest:
            raise ValueError("stored deletion plan integrity failure")
        return plan


@dataclass(frozen=True)
class _RecordRow:
    id: str
    plan_id: str
    plan_digest: str
    created_at: datetime


@dataclass(frozen=True)
class _ReceiptRow:
    intent_digest: str
    plan_id: str | None
    deletion_id: str | None


@dataclass(frozen=True)
class _LedgerRow:
    id: str
    completed_at: datetime
    deleted: list[list[str]]
    retained_independent_copies: list[list[str]]


async def _load_result(base: _Session, deletion_id: str) -> DeletionResult | None:
    row = await base._one(
        _LedgerRow,
        """
        SELECT id, completed_at, deleted, retained_independent_copies
        FROM {s}.deletion_ledger WHERE deletion_id = %s
    """,
        (deletion_id,),
    )
    if row is None:
        return None

    def targets(items: list[list[str]]) -> tuple[DeletionTarget, ...]:
        return tuple(
            DeletionTarget(
                cast(ResourceOwner, owner), cast(ResourceKind, kind), identity, int(revision)
            )
            for owner, kind, identity, revision in items
        )

    return DeletionResult(
        targets(row.deleted), targets(row.retained_independent_copies), row.id, row.completed_at
    )


class PostgresDeletionUnitOfWork:
    """Compose the existing private PG session; no second connection or transaction."""

    def __init__(self, versions: PostgresVersionUnitOfWork, *, meetings: bool = False) -> None:
        self._versions = versions
        self._meetings = meetings

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[DeletionSession]:
        async with self._versions.transaction() as session:
            assert isinstance(session, _Session)
            yield _DeletionSession(session, meetings=self._meetings)


class _DeletionSession:
    def __init__(self, base: _Session, *, meetings: bool = False) -> None:
        self._base = base
        self._meetings = meetings
        self._snapshots: dict[
            tuple[str, str, str], tuple[DeletionTarget, tuple[DeletionDependency, ...]]
        ] = {}
        self._claims: set[tuple[str, DeletionFamily, str]] = set()

    async def now(self) -> datetime:
        return await self._base.now()

    async def snapshot(
        self,
        workspace_id: str,
        target: DeletionTarget,
        mode: DeletionMode,
    ) -> tuple[DeletionTarget, tuple[DeletionDependency, ...]] | None:
        base = self._base
        if target.owner != "workbench" or target.kind not in {"project", "transcript"}:
            return None
        if target.kind == "project":
            project_id = target.id
        else:
            parent = await base._one(
                _Parent, "SELECT project_id FROM {s}.transcripts WHERE id = %s", (target.id,)
            )
            if parent is None:
                return None
            project_id = parent.project_id
        # All dependency creation also locks the project. Parent -> sorted children
        # is shared by preview/acceptance; transcript edits lock their own header.
        project = await base.get_project(project_id, for_update=True)
        if project is None or project.workspace_id != workspace_id:
            return None
        dependencies: list[DeletionDependency] = []
        if target.kind == "project":
            async with base._connection.cursor(row_factory=class_row(_Identity)) as cursor:
                await cursor.execute(
                    base._sql(
                        "SELECT id, head_revision FROM {s}.transcripts "
                        "WHERE project_id = %s ORDER BY id"
                    ),
                    (project_id,),
                )
                children = await cursor.fetchall()
            for child in children:
                text = await base.get_transcript(child.id, for_update=True)
                if text is not None:
                    dependencies.append(
                        DeletionDependency(
                            DeletionTarget("workbench", "transcript", text.id, text.head.number),
                            "owned_child",
                            "cascade",
                        )
                    )
                else:
                    # A tombstoned child still has an FK until its own cleanup ends.
                    dependencies.append(
                        DeletionDependency(
                            DeletionTarget(
                                "workbench", "transcript", child.id, int(child.head_revision)
                            ),
                            "owned_child",
                            "blocks",
                        )
                    )
            current = DeletionTarget("workbench", "project", project.id, project.metadata_revision)
        else:
            text = await base.get_transcript(target.id, for_update=True)
            if text is None:
                return None
            current = DeletionTarget("workbench", "transcript", text.id, text.head.number)
        if self._meetings:
            meetings = await base._many(
                _MeetingDependency,
                "SELECT id,workspace_id,version FROM {s}.meetings "
                "WHERE project_id=%s AND (%s OR transcript_id=%s) ORDER BY id FOR UPDATE",
                (project_id, target.kind == "project", target.id),
            )
            for meeting in meetings:
                if meeting.workspace_id != workspace_id:
                    raise ValueError("meeting dependency scope mismatch")
                dependencies.append(
                    DeletionDependency(
                        DeletionTarget("workbench", "meeting", meeting.id, int(meeting.version)),
                        "owned_child" if target.kind == "project" else "persistent_reference",
                        "blocks" if await base._deleted("meeting", meeting.id) else "cascade",
                    )
                )
        result = current, dependency_snapshot(tuple(dependencies), mode=mode)
        self._snapshots[(workspace_id, target.kind, target.id)] = result
        return result

    async def get_plan(self, workspace_id: str, plan_id: str) -> DeletionPlan | None:
        row = await self._base._one(
            _PlanRow,
            """
            SELECT id, workspace_id, target_kind, target_id, target_revision, mode,
                created_at, expires_at, dependencies, plan_digest FROM {s}.deletion_plans
            WHERE workspace_id = %s AND id = %s
        """,
            (workspace_id, plan_id),
        )
        return None if row is None else row.domain()

    async def save_plan(self, plan: DeletionPlan, digest: str) -> None:
        snapshot = self._snapshots.get((plan.workspace_id, plan.target.kind, plan.target.id))
        if snapshot != (plan.target, plan.dependencies) or digest != plan_digest(plan):
            raise ValueError("plan requires the complete locked snapshot")
        dependencies = [
            [
                item.target.owner,
                item.target.kind,
                item.target.id,
                str(item.target.revision),
                item.relation,
                item.disposition,
            ]
            for item in plan.dependencies
        ]
        await self._base._execute(
            """
            INSERT INTO {s}.deletion_plans (id, workspace_id, target_kind, target_id,
                target_revision, mode, created_at, expires_at, dependencies, plan_digest)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
            (
                plan.id,
                plan.workspace_id,
                plan.target.kind,
                plan.target.id,
                plan.target.revision,
                plan.mode,
                plan.created_at,
                plan.expires_at,
                Jsonb(dependencies),
                digest,
            ),
        )

    async def get_record(
        self,
        workspace_id: str,
        deletion_id: str,
        *,
        current: bool = False,
    ) -> DeletionRecord | None:
        row = await self._base._one(
            _RecordRow,
            """
            SELECT id, plan_id, plan_digest, created_at FROM {s}.deletion_records
            WHERE workspace_id = %s AND id = %s
        """,
            (workspace_id, deletion_id),
        )
        if row is None:
            return None
        result = await _load_result(self._base, deletion_id) if current else None
        return DeletionRecord(row.id, row.plan_id, row.plan_digest, row.created_at, result)

    async def accept(self, plan: DeletionPlan, record: DeletionRecord) -> None:
        if (
            self._snapshots.get((plan.workspace_id, plan.target.kind, plan.target.id))
            != (plan.target, plan.dependencies)
            or record.plan_id != plan.id
            or record.plan_digest != plan_digest(plan)
            or not plan.eligible
        ):
            raise ValueError("deletion requires the complete locked plan")
        await self._base._execute(
            """
            INSERT INTO {s}.deletion_records (id, workspace_id, plan_id, plan_digest, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """,
            (record.id, plan.workspace_id, plan.id, record.plan_digest, record.created_at),
        )
        for target in plan.confirmed_targets:
            await self._base._execute(
                """
                INSERT INTO {s}.resource_tombstones
                    (resource_kind, resource_id, resource_revision, deletion_id, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """,
                (target.kind, target.id, target.revision, record.id, record.created_at),
            )
        await self._base._execute(
            """
            INSERT INTO {s}.deletion_outbox (deletion_id, created_at) VALUES (%s, %s)
        """,
            (record.id, record.created_at),
        )

    def _key(self, workspace_id: str, family: DeletionFamily, key: str) -> str:
        if (
            not 1 <= len(workspace_id) <= 128
            or not 1 <= len(key) <= 128
            or family not in {"plan", "create"}
        ):
            raise ValueError("invalid deletion receipt identity")
        return hashlib.sha256(key.encode()).hexdigest()

    async def claim_receipt(
        self,
        workspace_id: str,
        family: DeletionFamily,
        key: str,
    ) -> DeletionReceipt | None:
        digest = self._key(workspace_id, family, key)
        lock = hashlib.sha256(
            json.dumps(
                [
                    "workbench-deletion",
                    self._base._schema.as_string(),
                    workspace_id,
                    family,
                    digest,
                ]
            ).encode()
        ).digest()[:8]
        await self._base._execute(
            "SELECT pg_advisory_xact_lock(%s)", (int.from_bytes(lock, signed=True),)
        )
        self._claims.add((workspace_id, family, digest))
        row = await self._base._one(
            _ReceiptRow,
            """
            SELECT intent_digest, plan_id, deletion_id FROM {s}.deletion_receipts
            WHERE workspace_id = %s AND family = %s AND key_digest = %s
        """,
            (workspace_id, family, digest),
        )
        if row is None:
            return None
        result: DeletionPlan | DeletionRecord | None
        if row.plan_id is not None:
            result = await self.get_plan(workspace_id, row.plan_id)
        else:
            assert row.deletion_id is not None
            result = await self.get_record(workspace_id, row.deletion_id)
        assert result is not None
        return DeletionReceipt(row.intent_digest, result)

    async def save_receipt(
        self,
        workspace_id: str,
        family: DeletionFamily,
        key: str,
        receipt: DeletionReceipt,
    ) -> None:
        digest = self._key(workspace_id, family, key)
        if (workspace_id, family, digest) not in self._claims:
            raise ValueError("deletion receipt must be claimed")
        plan_id, deletion_id = None, None
        if isinstance(receipt.result, DeletionPlan):
            if family != "plan" or receipt.result.workspace_id != workspace_id:
                raise ValueError("deletion receipt scope mismatch")
            plan_id = receipt.result.id
        else:
            if (
                family != "create"
                or await self.get_record(workspace_id, receipt.result.id) != receipt.result
            ):
                raise ValueError("deletion receipt snapshot mismatch")
            deletion_id = receipt.result.id
        await self._base._execute(
            """
            INSERT INTO {s}.deletion_receipts
                (workspace_id, family, key_digest, intent_digest, plan_id, deletion_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """,
            (workspace_id, family, digest, receipt.intent_digest, plan_id, deletion_id),
        )
