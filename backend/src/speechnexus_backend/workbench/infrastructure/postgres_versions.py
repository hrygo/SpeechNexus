"""Bounded PostgreSQL workbench transactions; no DDL, routes or speech internals.

Historical rows are append-only for the runtime role. Replay receipts reference
those rows rather than duplicating business content. Deletion must later handle
these references in the workbench's own versioned deletion transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.rows import class_row

from speechnexus_backend.workbench.application.listings import ListingPosition
from speechnexus_backend.workbench.application.version_ports import (
    ErasedMutationReceipt,
    MutationReceipt,
    MutationScope,
    VersionSession,
)
from speechnexus_backend.workbench.application.version_ports import (
    VersionStoreError as VersionStoreError,
)
from speechnexus_backend.workbench.domain.listings import TranscriptSummary
from speechnexus_backend.workbench.domain.versions import (
    Project,
    ProjectChanges,
    ProjectStatus,
    RevisionStatus,
    SpeechSource,
    Transcript,
    TranscriptRevision,
)
from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


@dataclass(frozen=True)
class _Clock:
    now: datetime


@dataclass(frozen=True)
class _ProjectHead:
    metadata_revision: Decimal


@dataclass(frozen=True)
class _Tombstone:
    present: bool


@dataclass(frozen=True)
class _ProjectRow:
    id: str
    workspace_id: str
    created_at: datetime
    revision: Decimal
    name: str
    description: str | None
    status: ProjectStatus
    updated_at: datetime

    def domain(self) -> Project:
        return Project(
            self.id,
            self.workspace_id,
            self.name,
            self.description,
            self.status,
            int(self.revision),
            self.created_at,
            self.updated_at,
        )


@dataclass(frozen=True)
class _TranscriptRow:
    id: str
    project_id: str
    display_name: str
    created_at: datetime
    head_revision: Decimal


@dataclass(frozen=True)
class _TranscriptListingRow:
    # A visible parent with no visible children yields one row with NULL child fields.
    parent_id: str
    id: str | None
    display_name: str | None
    created_at: datetime | None
    head_revision: Decimal | None
    updated_at: datetime | None
    source_artifact_id: str | None
    source_receipt_id: str | None

    def summary(self) -> TranscriptSummary:
        assert self.id is not None and self.display_name is not None
        assert self.created_at is not None and self.head_revision is not None
        assert self.updated_at is not None
        source = None
        if self.source_artifact_id is not None:
            assert self.source_receipt_id is not None
            source = SpeechSource(self.source_artifact_id, self.source_receipt_id)
        return TranscriptSummary(
            self.id,
            self.parent_id,
            self.display_name,
            int(self.head_revision),
            source,
            self.created_at,
            self.updated_at,
        )


@dataclass(frozen=True)
class _RevisionRow:
    id: str
    transcript_id: str
    revision: Decimal
    status: RevisionStatus
    text: str
    source_revision: Decimal | None
    source_artifact_id: str | None
    source_receipt_id: str | None
    created_at: datetime

    def domain(self) -> TranscriptRevision:
        source = None
        if self.source_artifact_id is not None:
            assert self.source_receipt_id is not None
            source = SpeechSource(self.source_artifact_id, self.source_receipt_id)
        return TranscriptRevision(
            self.id,
            self.transcript_id,
            int(self.revision),
            self.status,
            self.text,
            None if self.source_revision is None else int(self.source_revision),
            source,
            self.created_at,
        )


@dataclass(frozen=True)
class _ViewRevisionRow(_RevisionRow):
    project_id: str
    display_name: str
    transcript_created_at: datetime
    head_revision: Decimal


@dataclass(frozen=True)
class _ReceiptRow:
    intent_digest: str
    project_id: str | None
    project_revision: Decimal | None
    transcript_id: str | None
    transcript_revision: Decimal | None
    erased: bool


class PostgresVersionUnitOfWork:
    """One short transaction per connection; per-instance admission and DB time bounds.

    Caller supplies an owner-separated runtime DSN and a migrated workbench schema.
    No automatic retry can duplicate an uncertain COMMIT. Cancellation propagates;
    callers must reconcile any lost mutation reply with its original scoped key.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str,
        max_connections: int = 8,
        transaction_timeout: float = 10,
        lock_timeout_ms: int = 2000,
        statement_timeout_ms: int = 5000,
        cleanup_timeout: float = 1,
    ) -> None:
        self._schema = schema_identifier(schema)
        if not 1 <= max_connections <= 64 or not 0 < transaction_timeout <= 60:
            raise ValueError("invalid workbench connection bounds")
        if not 1 <= lock_timeout_ms <= statement_timeout_ms <= 60000:
            raise ValueError("invalid workbench database time bounds")
        if not 0 < cleanup_timeout <= 5:
            raise ValueError("invalid workbench cleanup bound")
        self._dsn = dsn
        self._slots = asyncio.Semaphore(max_connections)
        self._timeout = transaction_timeout
        self._lock_ms = lock_timeout_ms
        self._statement_ms = statement_timeout_ms
        self._cleanup_timeout = cleanup_timeout

    @staticmethod
    def _consume_exit_result(task: asyncio.Task[bool | None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _exit_transaction(
        self,
        connection: psycopg.AsyncConnection[tuple[object, ...]],
        context: AbstractAsyncContextManager[psycopg.AsyncTransaction],
        error: BaseException | None,
    ) -> None:
        # wait_for(task) can itself wait indefinitely for driver cancellation.
        # Wait without cancelling, then close the private socket BEFORE cancellation.
        task = asyncio.create_task(
            context.__aexit__(
                None if error is None else type(error),
                error,
                None if error is None else error.__traceback__,
            )
        )
        try:
            done, _ = await asyncio.wait({task}, timeout=self._cleanup_timeout)
            if not done:
                raise TimeoutError("workbench transaction exit deadline")
            task.result()
        finally:
            if not task.done():
                connection.pgconn.finish()
                task.cancel()
                task.add_done_callback(self._consume_exit_result)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[VersionSession]:
        committing = False
        session: _Session | None = None
        try:
            async with asyncio.timeout(self._timeout), self._slots:
                connection = await psycopg.AsyncConnection.connect(
                    self._dsn,
                    autocommit=True,
                    connect_timeout=5,
                )
                try:
                    # A DSN, PGOPTIONS or service-file role switch must not make a
                    # business connection a different identity than the login role the
                    # runtime preflight checked. Operate as the login role. RESET ROLE
                    # is a no-op here because a startup-packet -c role= becomes the
                    # session default; SET ROLE NONE always restores session_user.
                    await connection.execute("SET ROLE NONE")
                    context = connection.transaction()
                    await context.__aenter__()
                    try:
                        await connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                        await connection.execute("SET LOCAL search_path = pg_catalog")
                        await connection.execute(
                            "SELECT set_config('lock_timeout', %s, true), "
                            "set_config('statement_timeout', %s, true), "
                            "set_config('idle_in_transaction_session_timeout', %s, true), "
                            "set_config('TimeZone', 'UTC', true), "
                            "set_config('application_name', "
                            "'speechnexus-workbench-versions', true)",
                            (str(self._lock_ms), str(self._statement_ms), str(self._statement_ms)),
                        )
                        session = _Session(connection, self._schema)
                        yield session
                    except BaseException as error:
                        # A failed body never commits. Disconnect instead of issuing
                        # another network ROLLBACK; PG rolls back the private session.
                        connection.pgconn.finish()
                        # Preserve the original domain failure or cancellation even
                        # if the already-disconnected driver's exit is interrupted.
                        with suppress(BaseException):
                            await self._exit_transaction(connection, context, error)
                        raise
                    else:
                        committing = True
                        await self._exit_transaction(connection, context, None)
                finally:
                    # This connection is never pooled. No extra async rollback/logging
                    # on AsyncConnection.__aexit__ can extend cleanup or expose a query.
                    connection.pgconn.finish()
        except (psycopg.Error, TimeoutError) as error:
            state = error.sqlstate if isinstance(error, psycopg.Error) else None
            # Integrity errors, serialization failures and deadlocks prove abort.
            # Class 40 also contains 40003 (statement_completion_unknown), so it
            # cannot be accepted wholesale. Transport loss,
            # shutdown (57P01), or an unclassified COMMIT failure can follow a commit;
            # a SQLSTATE by itself is not proof of rollback.
            proven_abort = state is not None and (
                state.startswith("23") or state in {"40001", "40P01"}
            )
            unknown = committing and not proven_abort
            raise VersionStoreError(sqlstate=state, commit_unknown=unknown) from None
        finally:
            if session is not None:
                session.close()


class _Session:
    def __init__(
        self, connection: psycopg.AsyncConnection[tuple[object, ...]], schema: sql.Identifier
    ) -> None:
        self._connection = connection
        self._schema = schema
        self._active = True
        self._projects: dict[str, Project] = {}
        self._transcripts: dict[str, Transcript] = {}
        self._claims: set[tuple[MutationScope, str]] = set()

    def close(self) -> None:
        self._active = False
        self._projects.clear()
        self._transcripts.clear()
        self._claims.clear()

    def _sql(self, query: str) -> sql.Composed:
        if not self._active:
            raise RuntimeError("workbench transaction is closed")
        return sql.SQL(query).format(s=self._schema)

    async def _execute(self, query: str, params: Sequence[object] = ()) -> None:
        await self._connection.execute(self._sql(query), params)

    async def _one[T](
        self, factory: type[T], query: str, params: Sequence[object] = ()
    ) -> T | None:
        statement = self._sql(query)
        async with self._connection.cursor(row_factory=class_row(factory)) as cursor:
            await cursor.execute(statement, params)
            return await cursor.fetchone()

    async def _many[T](
        self, factory: type[T], query: str, params: Sequence[object]
    ) -> tuple[T, ...]:
        statement = self._sql(query)
        async with self._connection.cursor(row_factory=class_row(factory)) as cursor:
            await cursor.execute(statement, params)
            return tuple(await cursor.fetchall())

    async def list_projects(
        self, workspace_id: str, *, limit: int, after: ListingPosition | None
    ) -> tuple[Project, ...]:
        seek = "" if after is None else " AND (p.created_at, p.id) < (%s, %s)"
        params: list[object] = [workspace_id]
        if after is not None:
            params.extend((after.created_at, after.item_id))
        params.append(limit)
        rows = await self._many(
            _ProjectRow,
            """
            SELECT p.id, p.workspace_id, p.created_at, v.revision, v.name, v.description,
                v.status, v.updated_at
            FROM {s}.projects p JOIN {s}.project_versions v
                ON v.project_id = p.id AND v.revision = p.metadata_revision
            WHERE p.workspace_id = %s AND NOT EXISTS (
                SELECT 1 FROM {s}.resource_tombstones d
                WHERE d.resource_kind = 'project' AND d.resource_id = p.id
                UNION ALL
                SELECT 1 FROM {s}.deletion_ledger l, jsonb_array_elements(l.deleted) e
                WHERE e->>1 = 'project' AND e->>2 = p.id)
            """
            + seek
            + " ORDER BY p.created_at DESC, p.id DESC LIMIT %s",
            params,
        )
        return tuple(row.domain() for row in rows)

    async def list_transcripts(
        self, project_id: str, *, limit: int, after: ListingPosition | None
    ) -> tuple[TranscriptSummary, ...] | None:
        # One statement gives parent existence/tombstone and children the same MVCC
        # snapshot. A separate get_project() could race with deletion or cleanup.
        seek = "" if after is None else " AND (t.created_at, t.id) < (%s, %s)"
        params: list[object] = []
        if after is not None:
            params.extend((after.created_at, after.item_id))
        params.extend((limit, project_id))
        rows = await self._many(
            _TranscriptListingRow,
            """
            SELECT p.id AS parent_id, c.id, c.display_name, c.created_at,
                c.head_revision, c.updated_at, c.source_artifact_id, c.source_receipt_id
            FROM {s}.projects p
            LEFT JOIN LATERAL (
                SELECT t.id, t.display_name, t.created_at, t.head_revision,
                    v.created_at AS updated_at, v.source_artifact_id, v.source_receipt_id
                FROM {s}.transcripts t JOIN {s}.transcript_revisions v
                    ON v.transcript_id = t.id AND v.revision = t.head_revision
                WHERE t.project_id = p.id AND NOT EXISTS (
                    SELECT 1 FROM {s}.resource_tombstones d
                    WHERE d.resource_kind = 'transcript' AND d.resource_id = t.id
                    UNION ALL
                    SELECT 1 FROM {s}.deletion_ledger l, jsonb_array_elements(l.deleted) e
                    WHERE e->>1 = 'transcript' AND e->>2 = t.id)
                """
            + seek
            + """
                ORDER BY t.created_at DESC, t.id DESC LIMIT %s
            ) c ON true
            WHERE p.id = %s AND NOT EXISTS (
                SELECT 1 FROM {s}.resource_tombstones d
                WHERE d.resource_kind = 'project' AND d.resource_id = p.id
                UNION ALL
                SELECT 1 FROM {s}.deletion_ledger l, jsonb_array_elements(l.deleted) e
                WHERE e->>1 = 'project' AND e->>2 = p.id)
            ORDER BY c.created_at DESC, c.id DESC
            """,
            params,
        )
        if not rows:
            return None
        return tuple(row.summary() for row in rows if row.id is not None)

    async def now(self) -> datetime:
        # Called after locking reads; transaction_timestamp() could predate a lock wait.
        row = await self._one(_Clock, "SELECT clock_timestamp() AS now")
        assert row is not None
        return row.now

    async def _deleted(self, kind: str, identity: str) -> bool:
        row = await self._one(
            _Tombstone,
            """
            SELECT EXISTS (SELECT 1 FROM {s}.resource_tombstones
                WHERE resource_kind = %s AND resource_id = %s
                UNION ALL
                SELECT 1 FROM {s}.deletion_ledger l, jsonb_array_elements(l.deleted) e
                WHERE e->>1 = %s AND e->>2 = %s) AS present
        """,
            (kind, identity, kind, identity),
        )
        assert row is not None
        return row.present

    async def _project_at(self, project_id: str, revision: int) -> Project | None:
        if await self._deleted("project", project_id):
            return None
        row = await self._one(
            _ProjectRow,
            """
            SELECT p.id, p.workspace_id, p.created_at, v.revision, v.name, v.description,
                v.status, v.updated_at FROM {s}.projects p JOIN {s}.project_versions v
                ON v.project_id = p.id WHERE p.id = %s AND v.revision = %s
        """,
            (project_id, revision),
        )
        return None if row is None else row.domain()

    async def get_project(self, project_id: str, *, for_update: bool = False) -> Project | None:
        # Lock header before reading its immutable snapshot; a join read taken before a
        # lock wait could otherwise pair a newly visible head with an old statement view.
        suffix = " FOR UPDATE" if for_update else ""
        head = await self._one(
            _ProjectHead,
            "SELECT metadata_revision FROM {s}.projects WHERE id = %s" + suffix,
            (project_id,),
        )
        if head is None:
            return None
        # Separate post-lock visibility check: a predicate in the pre-wait SELECT
        # could miss a tombstone committed while this transaction waited for header.
        if await self._deleted("project", project_id):
            return None
        project = await self._project_at(project_id, int(head.metadata_revision))
        if project is None:
            return None
        if for_update:
            self._projects[project_id] = project
        return project

    async def save_project(self, project: Project) -> None:
        previous = self._projects.get(project.id)
        if previous is None:
            if await self.get_project(project.id) is not None:
                raise ValueError("project write requires a locked read")
            if project.metadata_revision != 1 or project != Project.create(
                project_id=project.id,
                workspace_id=project.workspace_id,
                name=project.name,
                description=project.description,
                now=project.created_at,
            ):
                raise ValueError("invalid initial project snapshot")
            await self._execute(
                """
                INSERT INTO {s}.projects (id, workspace_id, created_at, metadata_revision)
                VALUES (%s, %s, %s, %s)
            """,
                (project.id, project.workspace_id, project.created_at, 1),
            )
        elif project != previous.edit(
            expected_revision=previous.metadata_revision,
            changes=ProjectChanges(project.name, project.description, project.status),
            now=project.updated_at,
        ):
            raise ValueError("invalid project snapshot transition")
        await self._execute(
            """
            INSERT INTO {s}.project_versions
                (project_id, revision, name, description, status, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """,
            (
                project.id,
                project.metadata_revision,
                project.name,
                project.description,
                project.status,
                project.updated_at,
            ),
        )
        if previous is not None:
            await self._execute(
                """
                UPDATE {s}.projects SET metadata_revision = %s
                WHERE id = %s AND metadata_revision = %s
            """,
                (project.metadata_revision, project.id, previous.metadata_revision),
            )
        self._projects[project.id] = project

    async def _transcript_at(
        self, transcript_id: str, revision: int | None = None, *, for_update: bool = False
    ) -> Transcript | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = await self._one(
            _TranscriptRow,
            "SELECT id, project_id, display_name, created_at, head_revision "
            "FROM {s}.transcripts WHERE id = %s" + suffix,
            (transcript_id,),
        )
        if row is None:
            return None
        if await self._deleted("transcript", transcript_id):
            return None
        head = await self.get_revision(
            transcript_id, int(row.head_revision) if revision is None else revision
        )
        if head is None:
            return None
        return Transcript(row.id, row.project_id, row.display_name, row.created_at, head)

    async def get_transcript(
        self, transcript_id: str, *, for_update: bool = False
    ) -> Transcript | None:
        transcript = await self._transcript_at(transcript_id, for_update=for_update)
        if transcript is not None and for_update:
            self._transcripts[transcript_id] = transcript
        return transcript

    async def get_revision(self, transcript_id: str, revision: int) -> TranscriptRevision | None:
        if await self._deleted("transcript", transcript_id):
            return None
        row = await self._one(
            _RevisionRow,
            """
            SELECT id, transcript_id, revision, status, text, source_revision,
                source_artifact_id, source_receipt_id, created_at
            FROM {s}.transcript_revisions WHERE transcript_id = %s AND revision = %s
        """,
            (transcript_id, revision),
        )
        return None if row is None else row.domain()

    async def get_transcript_view(
        self, transcript_id: str, revision: int | None
    ) -> tuple[Transcript, TranscriptRevision] | None:
        # Both bodies, metadata and parent/child visibility use one MVCC statement
        # snapshot. Separate READ COMMITTED queries could pair an old head with a
        # newer selected revision or a deletion accepted between reads.
        rows = await self._many(
            _ViewRevisionRow,
            """
            SELECT v.id, v.transcript_id, v.revision, v.status, v.text,
                v.source_revision, v.source_artifact_id, v.source_receipt_id,
                v.created_at, t.project_id, t.display_name,
                t.created_at AS transcript_created_at, t.head_revision
            FROM {s}.transcripts t JOIN {s}.projects p ON p.id = t.project_id
            JOIN {s}.transcript_revisions v ON v.transcript_id = t.id
                AND (v.revision = t.head_revision OR v.revision = %s)
            WHERE t.id = %s AND NOT EXISTS (
                SELECT 1 FROM {s}.resource_tombstones d
                WHERE (d.resource_kind = 'project' AND d.resource_id = p.id)
                    OR (d.resource_kind = 'transcript' AND d.resource_id = t.id))
            """,
            (revision, transcript_id),
        )
        head = next((row for row in rows if row.revision == row.head_revision), None)
        if head is None:
            return None
        selected = (
            head
            if revision is None
            else next((row for row in rows if row.revision == revision), None)
        )
        if selected is None:
            return None
        current = Transcript(
            head.transcript_id,
            head.project_id,
            head.display_name,
            head.transcript_created_at,
            head.domain(),
        )
        return current, selected.domain()

    async def save_transcript(self, transcript: Transcript) -> None:
        previous = self._transcripts.get(transcript.id)
        head = transcript.head
        if previous is None:
            if await self.get_transcript(transcript.id) is not None:
                raise ValueError("transcript write requires a locked read")
            if transcript.project_id not in self._projects:
                raise ValueError("transcript creation requires a parent locked read")
            if transcript != Transcript.create(
                transcript_id=transcript.id,
                project_id=transcript.project_id,
                display_name=transcript.display_name,
                revision_id=head.id,
                text=head.text,
                now=transcript.created_at,
                source=head.source,
            ):
                raise ValueError("invalid initial transcript snapshot")
            await self._execute(
                """
                INSERT INTO {s}.transcripts
                    (id, project_id, display_name, created_at, head_revision)
                VALUES (%s, %s, %s, %s, 1)
            """,
                (
                    transcript.id,
                    transcript.project_id,
                    transcript.display_name,
                    transcript.created_at,
                ),
            )
        else:
            source = (
                None
                if head.source_revision is None
                else await self.get_revision(transcript.id, head.source_revision)
            )
            if source is None:
                raise ValueError("invalid transcript snapshot transition")
            expected = (
                previous.seal(
                    expected_revision=previous.head.number,
                    revision_id=head.id,
                    text=head.text,
                    now=head.created_at,
                )
                if head.status == "sealed"
                else previous.revise(
                    expected_revision=previous.head.number,
                    revision_id=head.id,
                    text=head.text,
                    now=head.created_at,
                    source_revision=source,
                )
            )
            if transcript != expected:
                raise ValueError("invalid transcript snapshot transition")
        await self._execute(
            """
            INSERT INTO {s}.transcript_revisions (id, transcript_id, revision, status, text,
                source_revision, source_artifact_id, source_receipt_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
            (
                head.id,
                transcript.id,
                head.number,
                head.status,
                head.text,
                head.source_revision,
                None if head.source is None else head.source.artifact_id,
                None if head.source is None else head.source.receipt_id,
                head.created_at,
            ),
        )
        if previous is not None:
            await self._execute(
                """
                UPDATE {s}.transcripts SET head_revision = %s WHERE id = %s AND head_revision = %s
            """,
                (head.number, transcript.id, previous.head.number),
            )
        self._transcripts[transcript.id] = transcript

    @staticmethod
    def _receipt_key(scope: MutationScope, key: str) -> str:
        if (
            scope[0]
            not in {
                "project.create",
                "project.edit",
                "transcript.create",
                "transcript.revise",
                "transcript.seal",
            }
            or not 1 <= len(scope[1]) <= 128
            or not 1 <= len(key) <= 128
        ):
            raise ValueError("invalid workbench receipt identity")
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    async def claim_receipt(
        self, scope: MutationScope, key: str
    ) -> MutationReceipt | ErasedMutationReceipt | None:
        digest = self._receipt_key(scope, key)
        # Hash is only a transaction-lock bucket; the complete scoped digest is the PK.
        # A bucket collision adds contention but cannot alias a replay identity.
        lock_bytes = hashlib.sha256(
            json.dumps([self._schema.as_string(), *scope, digest], separators=(",", ":")).encode(
                "utf-8"
            )
        ).digest()[:8]
        lock_id = int.from_bytes(lock_bytes, signed=True)
        await self._execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
        self._claims.add((scope, digest))
        # Cleanup moves receipts atomically. Read both tables in one statement so
        # READ COMMITTED cannot observe a gap between two different snapshots.
        row = await self._one(
            _ReceiptRow,
            """
            SELECT * FROM (
                SELECT intent_digest, project_id, project_revision, transcript_id,
                    transcript_revision, false AS erased
                FROM {s}.mutation_receipts
                WHERE operation = %s AND scope_id = %s AND key_digest = %s
                UNION ALL
                SELECT intent_digest, NULL::text, NULL::numeric, NULL::text,
                    NULL::numeric, true AS erased
                FROM {s}.erased_mutation_receipts
                WHERE operation = %s AND scope_id = %s AND key_digest = %s
            ) AS candidates ORDER BY erased DESC LIMIT 1
        """,
            (*scope, digest, *scope, digest),
        )
        if row is None:
            return None
        if row.erased:
            return ErasedMutationReceipt(row.intent_digest)
        snapshot: Project | Transcript | None
        if row.project_id is not None:
            assert row.project_revision is not None
            snapshot = await self._project_at(row.project_id, int(row.project_revision))
        else:
            assert row.transcript_id is not None and row.transcript_revision is not None
            snapshot = await self._transcript_at(row.transcript_id, int(row.transcript_revision))
        if snapshot is None:
            kind = "project" if row.project_id is not None else "transcript"
            identity = row.project_id if row.project_id is not None else row.transcript_id
            assert identity is not None and await self._deleted(kind, identity)
            return ErasedMutationReceipt(row.intent_digest)
        return MutationReceipt(row.intent_digest, snapshot)

    async def save_receipt(self, scope: MutationScope, key: str, receipt: MutationReceipt) -> None:
        digest = self._receipt_key(scope, key)
        if (scope, digest) not in self._claims:
            raise ValueError("receipt must be claimed in this transaction")
        snapshot = receipt.snapshot
        target: tuple[str | int | None, ...]
        stored: Project | Transcript | None
        if isinstance(snapshot, Project):
            stored = await self._project_at(snapshot.id, snapshot.metadata_revision)
            target = (snapshot.id, snapshot.metadata_revision, None, None)
            valid_scope = scope in {
                ("project.create", snapshot.workspace_id),
                ("project.edit", snapshot.id),
            }
        else:
            stored = await self._transcript_at(snapshot.id, snapshot.head.number)
            target = (None, None, snapshot.id, snapshot.head.number)
            valid_scope = scope in {
                ("transcript.create", snapshot.project_id),
                ("transcript.revise", snapshot.id),
                ("transcript.seal", snapshot.id),
            }
            if scope[0] == "transcript.seal":
                valid_scope = (
                    valid_scope and snapshot.head.status == "sealed" and snapshot.head.number > 1
                )
        if stored != snapshot or not valid_scope:
            raise ValueError("receipt snapshot does not match persisted scope and version")
        await self._execute(
            """
            INSERT INTO {s}.mutation_receipts (operation, scope_id, key_digest, intent_digest,
                project_id, project_revision, transcript_id, transcript_revision)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
            (*scope, digest, receipt.intent_digest, *target),
        )
