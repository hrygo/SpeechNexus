"""Book and chapter rows with their scoped request receipts in one bounded transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import uuid4

from speechnexus_backend.workbench.application.books import OrdinalConflictError
from speechnexus_backend.workbench.application.listings import ListingPosition
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.books import Book, BookStatus, Chapter
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)


@dataclass(frozen=True)
class _BookRow:
    id: str
    project_id: str
    title: str
    status: str
    casting_revision: Decimal
    source_text: str | None
    created_at: datetime
    updated_at: datetime

    def domain(self) -> Book:
        return Book(
            id=self.id,
            project_id=self.project_id,
            title=self.title,
            status=cast(BookStatus, self.status),
            casting_revision=int(self.casting_revision),
            created_at=self.created_at,
            updated_at=self.updated_at,
            source_text=self.source_text,
        )


@dataclass(frozen=True)
class _BookListingRow:
    # A visible project with no visible books yields one row with NULL book fields.
    parent_id: str
    id: str | None
    title: str | None
    status: str | None
    casting_revision: Decimal | None
    source_text: str | None
    created_at: datetime | None
    updated_at: datetime | None

    def domain(self) -> Book:
        assert self.id is not None and self.title is not None and self.status is not None
        assert self.casting_revision is not None and self.created_at is not None
        assert self.updated_at is not None
        return Book(
            id=self.id,
            project_id=self.parent_id,
            title=self.title,
            status=cast(BookStatus, self.status),
            casting_revision=int(self.casting_revision),
            created_at=self.created_at,
            updated_at=self.updated_at,
            source_text=self.source_text,
        )


@dataclass(frozen=True)
class _ChapterRow:
    id: str
    book_id: str
    title: str
    ordinal: Decimal
    text: str
    text_revision: Decimal
    created_at: datetime

    def domain(self) -> Chapter:
        return Chapter(
            id=self.id,
            book_id=self.book_id,
            title=self.title,
            ordinal=int(self.ordinal),
            text=self.text,
            text_revision=int(self.text_revision),
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class _ChapterListingRow:
    # A visible book with no visible chapters yields one row with NULL chapter fields.
    parent_id: str
    id: str | None
    title: str | None
    ordinal: Decimal | None
    text: str | None
    text_revision: Decimal | None
    created_at: datetime | None

    def domain(self) -> Chapter:
        assert self.id is not None and self.title is not None and self.ordinal is not None
        assert self.text is not None and self.text_revision is not None
        assert self.created_at is not None
        return Chapter(
            id=self.id,
            book_id=self.parent_id,
            title=self.title,
            ordinal=int(self.ordinal),
            text=self.text,
            text_revision=int(self.text_revision),
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class _ReceiptRow:
    intent_digest: str
    book_id: str | None
    chapter_id: str | None


@dataclass(frozen=True)
class _Taken:
    taken: int


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class PostgresBooks:
    """The project row is the shared serialization point; no advisory lock is added."""

    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._versions = versions

    async def _project(
        self, base: _Session, project_id: str, workspace_id: str, *, lock: bool
    ) -> None:
        # A tombstoned project is invisible here, so deletion wins without extra state.
        project = await base.get_project(project_id, for_update=lock)
        if project is None or project.workspace_id != workspace_id:
            raise ResourceNotFoundError

    async def _book(self, base: _Session, book_id: str) -> _BookRow | None:
        return await base._one(
            _BookRow,
            """
            SELECT id, project_id, title, status, casting_revision, source_text,
                created_at, updated_at
            FROM {s}.books WHERE id = %s
        """,
            (book_id,),
        )

    async def _chapter(self, base: _Session, chapter_id: str) -> _ChapterRow | None:
        return await base._one(
            _ChapterRow,
            """
            SELECT id, book_id, title, ordinal, text, text_revision, created_at
            FROM {s}.chapters WHERE id = %s
        """,
            (chapter_id,),
        )

    async def _receipt(
        self, base: _Session, kind: str, scope_id: str, key_digest: str
    ) -> _ReceiptRow | None:
        return await base._one(
            _ReceiptRow,
            """
            SELECT intent_digest, book_id, chapter_id FROM {s}.book_receipts
            WHERE scope_kind = %s AND scope_id = %s AND key_digest = %s
        """,
            (kind, scope_id, key_digest),
        )

    @staticmethod
    def _notes(key: str | None, intent: list[object]) -> tuple[str | None, str]:
        if key is None:
            return None, ""
        return hashlib.sha256(key.encode("utf-8")).hexdigest(), _digest(intent)

    async def _note(
        self,
        base: _Session,
        kind: str,
        scope_id: str,
        key_digest: str | None,
        intent_digest: str,
        *,
        book_id: str | None,
        chapter_id: str | None,
        now: datetime,
    ) -> None:
        if key_digest is None:
            return
        await base._execute(
            """
            INSERT INTO {s}.book_receipts
                (scope_kind, scope_id, key_digest, intent_digest, book_id, chapter_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
            (kind, scope_id, key_digest, intent_digest, book_id, chapter_id, now),
        )

    async def create_book(
        self,
        *,
        project_id: str,
        workspace_id: str,
        title: str,
        source_text: str | None,
        key: str | None,
    ) -> Book:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            await self._project(base, project_id, workspace_id, lock=True)
            digest, intent = self._notes(key, ["book.create", title, source_text])
            if digest is not None:
                stored = await self._receipt(base, "book", project_id, digest)
                if stored is not None:
                    if stored.intent_digest != intent:
                        raise IdempotencyConflictError
                    # A receipt cannot outlive its row, so its absence is resource absence.
                    if stored.book_id is None:
                        raise ResourceNotFoundError
                    previous = await self._book(base, stored.book_id)
                    if previous is None:
                        raise ResourceNotFoundError
                    return previous.domain()
            now = await base.now()
            book = Book.create(
                book_id=uuid4().hex,
                project_id=project_id,
                title=title,
                source_text=source_text,
                now=now,
            )
            await base._execute(
                """
                INSERT INTO {s}.books
                    (id, project_id, title, status, casting_revision, source_text,
                        created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    book.id,
                    book.project_id,
                    book.title,
                    book.status,
                    book.casting_revision,
                    book.source_text,
                    book.created_at,
                    book.updated_at,
                ),
            )
            await self._note(
                base,
                "book",
                project_id,
                digest,
                intent,
                book_id=book.id,
                chapter_id=None,
                now=now,
            )
            return book

    async def list_books(
        self, *, project_id: str, workspace_id: str, limit: int, after: ListingPosition | None
    ) -> tuple[Book, ...] | None:
        # One statement gives the parent existence check and its books the same snapshot.
        seek = "" if after is None else " AND (b.created_at, b.id) < (%s, %s)"
        params: list[object] = []
        if after is not None:
            params.extend((after.created_at, after.item_id))
        params.extend((limit, project_id, workspace_id))
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            rows = await base._many(
                _BookListingRow,
                """
                SELECT p.id AS parent_id, b.id, b.title, b.status, b.casting_revision,
                    b.source_text, b.created_at, b.updated_at
                FROM {s}.projects p
                LEFT JOIN LATERAL (
                    SELECT b.id, b.title, b.status, b.casting_revision, b.source_text,
                        b.created_at, b.updated_at
                    FROM {s}.books b
                    WHERE b.project_id = p.id
                """
                + seek
                + """
                    ORDER BY b.created_at DESC, b.id DESC LIMIT %s
                ) b ON true
                WHERE p.id = %s AND p.workspace_id = %s AND NOT EXISTS (
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
        return tuple(row.domain() for row in rows if row.id is not None)

    async def get_book(self, *, book_id: str, workspace_id: str) -> Book:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._book(base, book_id)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=False)
            return row.domain()

    async def create_chapter(
        self,
        *,
        book_id: str,
        workspace_id: str,
        title: str,
        ordinal: int,
        text: str,
        key: str | None,
    ) -> Chapter:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            parent = await self._book(base, book_id)
            if parent is None:
                raise ResourceNotFoundError
            await self._project(base, parent.project_id, workspace_id, lock=True)
            # Deleting the book cascades its chapters, so re-read under the project lock:
            # the exclusive position check below is only authoritative while it holds.
            parent = await self._book(base, book_id)
            if parent is None:
                raise ResourceNotFoundError
            digest, intent = self._notes(key, ["chapter.create", book_id, title, ordinal, text])
            if digest is not None:
                stored = await self._receipt(base, "chapter", book_id, digest)
                if stored is not None:
                    if stored.intent_digest != intent:
                        raise IdempotencyConflictError
                    if stored.chapter_id is None:
                        raise ResourceNotFoundError
                    previous = await self._chapter(base, stored.chapter_id)
                    if previous is None:
                        raise ResourceNotFoundError
                    return previous.domain()
            taken = await base._one(
                _Taken,
                """
                SELECT 1 AS taken FROM {s}.chapters WHERE book_id = %s AND ordinal = %s
            """,
                (book_id, ordinal),
            )
            if taken is not None:
                raise OrdinalConflictError
            now = await base.now()
            chapter = Chapter.create(
                chapter_id=uuid4().hex,
                book_id=book_id,
                title=title,
                ordinal=ordinal,
                text=text,
                now=now,
            )
            await base._execute(
                """
                INSERT INTO {s}.chapters
                    (id, book_id, title, ordinal, text, text_revision, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    chapter.id,
                    chapter.book_id,
                    chapter.title,
                    chapter.ordinal,
                    chapter.text,
                    chapter.text_revision,
                    chapter.created_at,
                ),
            )
            await self._note(
                base,
                "chapter",
                book_id,
                digest,
                intent,
                book_id=None,
                chapter_id=chapter.id,
                now=now,
            )
            return chapter

    async def list_chapters(
        self, *, book_id: str, workspace_id: str, limit: int, after: ListingPosition | None
    ) -> tuple[Chapter, ...] | None:
        # The book's own visibility and its chapters are read under one snapshot.
        seek = "" if after is None else " AND (c.created_at, c.id) < (%s, %s)"
        params: list[object] = []
        if after is not None:
            params.extend((after.created_at, after.item_id))
        params.extend((limit, book_id, workspace_id))
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            rows = await base._many(
                _ChapterListingRow,
                """
                SELECT b.id AS parent_id, c.id, c.title, c.ordinal, c.text,
                    c.text_revision, c.created_at
                FROM {s}.books b
                JOIN {s}.projects p ON p.id = b.project_id
                LEFT JOIN LATERAL (
                    SELECT c.id, c.title, c.ordinal, c.text, c.text_revision, c.created_at
                    FROM {s}.chapters c
                    WHERE c.book_id = b.id
                """
                + seek
                + """
                    ORDER BY c.created_at DESC, c.id DESC LIMIT %s
                ) c ON true
                WHERE b.id = %s AND p.workspace_id = %s AND NOT EXISTS (
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
        return tuple(row.domain() for row in rows if row.id is not None)

    async def get_chapter(self, *, chapter_id: str, workspace_id: str) -> Chapter:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._chapter(base, chapter_id)
            if row is None:
                raise ResourceNotFoundError
            parent = await self._book(base, row.book_id)
            if parent is None:
                raise ResourceNotFoundError
            await self._project(base, parent.project_id, workspace_id, lock=False)
            return row.domain()
