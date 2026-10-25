"""Erase only confirmed, tombstoned workbench content and publish completion evidence.

Row erasure and its immutable ledger stay in one bounded transaction. The workbench's own
saved-asset objects are erased after that commit, through an injected object zone: the
project prefix still identifies them, so an at-least-once retry finishes the job without
holding a transaction across object I/O. Public success additionally requires the object
phase marker. Execution stop evidence remains out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from speechnexus_backend.workbench.application.saved_assets import (
    ObjectZone,
    ObjectZoneUnavailableError,
)
from speechnexus_backend.workbench.domain.deletion import DeletionResult, DeletionTarget
from speechnexus_backend.workbench.domain.saved_assets import project_object_prefix
from speechnexus_backend.workbench.infrastructure.postgres_deletions import (
    _DeletionSession,
    _load_result,
    _Parent,
)
from speechnexus_backend.workbench.infrastructure.postgres_owned_deletions import (
    PostgresOwnedDeletionCatalog,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)


@dataclass(frozen=True)
class _Pending:
    workspace_id: str
    plan_id: str
    plan_digest: str
    completed_at: datetime | None
    objects_erased_at: datetime | None


@dataclass(frozen=True)
class _PendingId:
    deletion_id: str


# Once an object phase is owed, omitting its adapter on restart cannot settle it.
# A genuinely inapplicable object phase is marked inside the row transaction.
_PENDING_OBJECTS = """
    SELECT o.deletion_id FROM {s}.deletion_outbox o
    WHERE o.completed_at IS NULL OR o.objects_erased_at IS NULL
    ORDER BY o.created_at, o.deletion_id LIMIT %s
"""


@dataclass(frozen=True)
class _Project:
    workspace_id: str
    metadata_revision: Decimal


@dataclass(frozen=True)
class _Child:
    id: str
    head_revision: Decimal


@dataclass(frozen=True)
class _Meeting:
    id: str
    version: Decimal


@dataclass(frozen=True)
class _Pin:
    resource_revision: Decimal
    deletion_id: str


@dataclass(frozen=True)
class _Exists:
    present: bool


class PostgresDeletionCleanup:
    """At-least-once outbox consumer; role grants cannot erase its completion evidence."""

    def __init__(
        self,
        unit_of_work: PostgresVersionUnitOfWork,
        *,
        meetings: bool = False,
        objects: ObjectZone | None = None,
        owned: PostgresOwnedDeletionCatalog | None = None,
    ) -> None:
        self._uow = unit_of_work
        self._meetings = meetings
        self._objects = objects
        self._owned = owned

    async def pending(self, *, limit: int) -> tuple[str, ...]:
        """Accepted deletions whose erasure is still owed, oldest accepted first.

        A deletion whose rows are erased but whose object phase is not settled is
        still owed, including after a restart without the required object adapter.
        """
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError("pending deletion limit outside the supported range")
        async with self._uow.transaction() as base:
            assert isinstance(base, _Session)
            rows = await base._many(
                _PendingId,
                _PENDING_OBJECTS,
                (limit,),
            )
        return tuple(row.deletion_id for row in rows)

    async def run(self, deletion_id: str) -> DeletionResult | None:
        """Erase rows, then the matching objects, and return only complete evidence."""
        result, project_id, objects_owed = await self._erase_rows(deletion_id)
        if result is None:
            return None
        if project_id is not None and objects_owed:
            if self._objects is None:
                raise ObjectZoneUnavailableError
            # Idempotent and re-runnable: a retry after this point erases the leftovers,
            # and only a finished sweep publishes the object half of the evidence.
            for prefix in await self._object_prefixes(deletion_id, project_id):
                await self._objects.erase_prefix(prefix=prefix)
            await self._record_objects_erased(deletion_id)
        async with self._uow.transaction() as base:
            assert isinstance(base, _Session)
            complete = await _load_result(base, deletion_id, complete_only=True)
        if complete is None:
            raise ValueError("deletion object phase remains incomplete")
        return complete

    async def _object_prefixes(self, deletion_id: str, project_id: str) -> tuple[str, ...]:
        """Recover exactly the prefixes pinned before the owned rows were erased."""
        prefixes = {project_object_prefix(project_id)}
        if self._owned is not None:
            async with self._uow.transaction() as base:
                assert isinstance(base, _Session)
                pending = await base._one(
                    _Pending,
                    "SELECT r.workspace_id,r.plan_id,r.plan_digest,o.completed_at,"
                    "o.objects_erased_at FROM {s}.deletion_records r "
                    "JOIN {s}.deletion_outbox o ON o.deletion_id=r.id WHERE r.id=%s",
                    (deletion_id,),
                )
                if pending is None:
                    raise ValueError("accepted deletion evidence missing")
                content = await self._owned.stored(base, pending.workspace_id, pending.plan_id)
                if content is None or content.get("project_id") != project_id:
                    raise ValueError("accepted owned deletion snapshot missing")
                prefixes.update(content["object_prefixes"])
        return tuple(sorted(prefixes))

    async def _record_objects_erased(self, deletion_id: str) -> None:
        """Publish the object half after the sweep, outside the row transaction."""
        async with self._uow.transaction() as base:
            assert isinstance(base, _Session)
            stamped = await base.now()
            await base._execute(
                """
                UPDATE {s}.deletion_outbox SET objects_erased_at = %s
                WHERE deletion_id = %s AND objects_erased_at IS NULL
            """,
                (stamped, deletion_id),
            )

    async def _erased_project(self, base: _Session, pending: _Pending) -> str | None:
        """Project prefix of an accepted plan, so a retry can finish object erasure."""
        plan = await _DeletionSession(base).get_plan(pending.workspace_id, pending.plan_id)
        if plan is None:
            raise ValueError("accepted deletion plan missing")
        if plan.target.kind != "project":
            return None
        return plan.target.id

    async def _erase_rows(self, deletion_id: str) -> tuple[DeletionResult | None, str | None, bool]:
        async with self._uow.transaction() as base:
            assert isinstance(base, _Session)
            pending = await base._one(
                _Pending,
                """
                SELECT r.workspace_id, r.plan_id, r.plan_digest, o.completed_at,
                    o.objects_erased_at
                FROM {s}.deletion_outbox o JOIN {s}.deletion_records r ON r.id = o.deletion_id
                WHERE o.deletion_id = %s FOR UPDATE OF o
            """,
                (deletion_id,),
            )
            if pending is None:
                return None, None, False
            # A second connection waiting on the outbox must read evidence AFTER
            # acquiring its lock; the first worker may have committed while it waited.
            previous = await _load_result(base, deletion_id)
            if previous is not None:
                if pending.completed_at != previous.completed_at:
                    raise ValueError("deletion completion evidence mismatch")
                project_id = await self._erased_project(base, pending)
                if project_id is None and pending.objects_erased_at is None:
                    # Legacy transcript row evidence has no project object phase.
                    # Persist that decision so restarts cannot endlessly requeue it.
                    await base._execute(
                        "UPDATE {s}.deletion_outbox SET objects_erased_at = %s "
                        "WHERE deletion_id = %s AND objects_erased_at IS NULL",
                        (pending.completed_at, deletion_id),
                    )
                    return previous, None, False
                return previous, project_id, pending.objects_erased_at is None
            if pending.completed_at is not None:
                raise ValueError("deletion completion ledger missing")
            plan = await _DeletionSession(base).get_plan(pending.workspace_id, pending.plan_id)
            if plan is None or not plan.eligible:
                raise ValueError("accepted deletion plan missing or ineligible")
            if plan.target.kind == "project":
                project_id = plan.target.id
            elif plan.target.kind == "transcript":
                parent = await base._one(
                    _Parent,
                    "SELECT project_id FROM {s}.transcripts WHERE id = %s",
                    (plan.target.id,),
                )
                if parent is None:
                    raise ValueError("accepted deletion content missing without evidence")
                project_id = parent.project_id
            else:
                raise ValueError("cleanup adapter not available for this resource")
            project = await base._one(
                _Project,
                """
                SELECT workspace_id, metadata_revision FROM {s}.projects WHERE id = %s FOR UPDATE
            """,
                (project_id,),
            )
            if project is None or project.workspace_id != pending.workspace_id:
                raise ValueError("accepted deletion parent mismatch")
            owned_prefixes: tuple[str, ...] = ()
            owned_revisions: dict[str, int] = {}
            if self._owned is not None and plan.target.kind == "project":
                stored = await self._owned.stored(base, pending.workspace_id, pending.plan_id)
                current_owned = await self._owned.snapshot(base, project_id)
                if stored is None or stored != current_owned.content:
                    raise ValueError("owned content changed after accepted deletion")
                if any(item.disposition == "blocks" for item in current_owned.dependencies):
                    raise ValueError("active execution or independent reference prevents cleanup")
                current_targets = {
                    item.target
                    for item in current_owned.dependencies
                    if item.relation == "owned_child"
                }
                confirmed = {
                    target
                    for target in plan.confirmed_targets
                    if target.kind in {"audiobook", "saved_asset"}
                }
                if current_targets != confirmed:
                    raise ValueError("unconfirmed owned content prevents cleanup")
                owned_revisions = {target.id: target.revision for target in current_targets}
                owned_prefixes = current_owned.object_prefixes
            transcripts = sorted(t.id for t in plan.confirmed_targets if t.kind == "transcript")
            async with base._connection.cursor(row_factory=class_row(_Child)) as cursor:
                await cursor.execute(
                    base._sql("""
                    SELECT id, head_revision FROM {s}.transcripts WHERE project_id = %s
                    AND (id = ANY(%s::text[]) OR %s) ORDER BY id FOR UPDATE
                """),
                    (project_id, transcripts, plan.target.kind == "project"),
                )
                children = await cursor.fetchall()
            if [child.id for child in children] != transcripts:
                raise ValueError("unconfirmed or missing child prevents cleanup")
            revisions = {child.id: int(child.head_revision) for child in children}
            revisions.update(owned_revisions)
            meetings = sorted(t.id for t in plan.confirmed_targets if t.kind == "meeting")
            if self._meetings:
                meeting_rows = await base._many(
                    _Meeting,
                    "SELECT id,version FROM {s}.meetings WHERE project_id=%s "
                    "AND (%s OR transcript_id=ANY(%s::text[])) ORDER BY id FOR UPDATE",
                    (project_id, plan.target.kind == "project", transcripts),
                )
                if [row.id for row in meeting_rows] != meetings:
                    raise ValueError("unconfirmed or missing meeting prevents cleanup")
                revisions.update({row.id: int(row.version) for row in meeting_rows})
            elif meetings:
                raise ValueError("meeting cleanup requires explicit adapter configuration")
            for target in plan.confirmed_targets:
                pin = await base._one(
                    _Pin,
                    """
                    SELECT resource_revision, deletion_id FROM {s}.resource_tombstones
                    WHERE resource_kind = %s AND resource_id = %s
                """,
                    (target.kind, target.id),
                )
                current = (
                    int(project.metadata_revision)
                    if target.kind == "project"
                    else revisions.get(target.id)
                )
                if (
                    pin is None
                    or pin.deletion_id != deletion_id
                    or int(pin.resource_revision) != target.revision
                    or current != target.revision
                ):
                    raise ValueError("cleanup requires exact accepted tombstone and version")

            # Retain only the scoped intent hashes; no business snapshot remains replayable.
            await base._execute(
                """
                INSERT INTO {s}.erased_mutation_receipts
                    (operation, scope_id, key_digest, intent_digest, deletion_id)
                SELECT m.operation, m.scope_id, m.key_digest, m.intent_digest, t.deletion_id
                FROM {s}.mutation_receipts m JOIN {s}.resource_tombstones t
                    ON (t.resource_kind = 'project' AND m.project_id = t.resource_id)
                    OR (t.resource_kind = 'transcript' AND m.transcript_id = t.resource_id)
                WHERE t.deletion_id = %s
            """,
                (deletion_id,),
            )
            await base._execute(
                """
                DELETE FROM {s}.mutation_receipts
                WHERE transcript_id = ANY(%s::text[]) OR (project_id = %s AND %s)
            """,
                (transcripts, project_id, plan.target.kind == "project"),
            )
            if self._meetings:
                # Remove only exact confirmed references before their source revisions
                # and minutes tasks. Any execution guard failure rolls this back too.
                await base._execute(
                    "DELETE FROM {s}.meetings WHERE id=ANY(%s::text[])", (meetings,)
                )
            await base._execute(
                """
                DELETE FROM {s}.transcript_revisions WHERE transcript_id = ANY(%s::text[])
            """,
                (transcripts,),
            )
            await base._execute(
                "DELETE FROM {s}.transcripts WHERE id = ANY(%s::text[])", (transcripts,)
            )
            if plan.target.kind == "project":
                await base._execute(
                    "DELETE FROM {s}.project_versions WHERE project_id = %s", (project_id,)
                )
                await base._execute("DELETE FROM {s}.projects WHERE id = %s", (project_id,))
            # Row cascades always remove the project's saved-asset rows. The explicit
            # existence check is admitted only with the object zone that erases bytes.
            saved_presence = (
                "UNION ALL SELECT 1 FROM {s}.saved_assets WHERE project_id = %s AND %s"
                if self._objects is not None
                else ""
            )
            left = await base._one(
                _Exists,
                "SELECT EXISTS (SELECT 1 FROM {s}.transcripts WHERE id = ANY(%s::text[])"
                " UNION ALL SELECT 1 FROM {s}.projects WHERE id = %s AND %s "
                + saved_presence
                + ") AS present",
                (transcripts, project_id, plan.target.kind == "project")
                + (
                    (project_id, plan.target.kind == "project") if self._objects is not None else ()
                ),
            )
            if left is None or left.present:
                raise ValueError("deletion has incomplete content erasure")
            completed = await base.now()
            retained = tuple(
                item.target
                for item in plan.dependencies
                if item.relation == "independent_copy" and item.disposition == "retain"
            )
            result = DeletionResult(plan.confirmed_targets, retained, uuid4().hex, completed)

            def encode(items: tuple[DeletionTarget, ...]) -> list[list[str]]:
                return [[t.owner, t.kind, t.id, str(t.revision)] for t in items]

            await base._execute(
                """
                INSERT INTO {s}.deletion_ledger
                    (id, deletion_id, completed_at, deleted, retained_independent_copies)
                VALUES (%s, %s, %s, %s, %s)
            """,
                (
                    result.ledger_id,
                    deletion_id,
                    completed,
                    Jsonb(encode(result.deleted)),
                    Jsonb(encode(retained)),
                ),
            )
            objects_owed = plan.target.kind == "project" and (
                self._objects is not None or bool(owned_prefixes)
            )
            await base._execute(
                """
                UPDATE {s}.deletion_outbox SET completed_at = %s, objects_erased_at = %s
                WHERE deletion_id = %s
            """,
                (completed, None if objects_owed else completed, deletion_id),
            )
            return result, project_id if plan.target.kind == "project" else None, objects_owed
