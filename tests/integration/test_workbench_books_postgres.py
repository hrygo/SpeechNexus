"""Book and chapter composition over the disposable PostgreSQL store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_deletions_postgres import content, target
from tests.integration.test_workbench_deletions_postgres import drop_tombstones as drop_tombstones
from tests.integration.test_workbench_deletions_postgres import ledger_entries as ledger_entries
from tests.integration.test_workbench_deletions_postgres import restore_rows as restore_rows
from tests.integration.test_workbench_deletions_postgres import service as deletions_service
from tests.integration.test_workbench_deletions_postgres import snapshot_rows as snapshot_rows
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.books import (
    OrdinalConflictError,
    WorkbenchBooks,
)
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError
from speechnexus_backend.workbench.infrastructure.books_schema import (
    books_migration_sql,
    grant_books_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.postgres_books import PostgresBooks
from speechnexus_backend.workbench.infrastructure.postgres_deletion_cleanup import (
    PostgresDeletionCleanup,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import PostgresVersionUnitOfWork

pytestmark = pytest.mark.integration
WORKSPACE = "workspace-books"
BOOK_KEY = "book-request-key-0001"
CHAPTER_KEY = "chapter-request-key-0001"
SOURCE_TEXT = "私有书稿正文：合成样例"
CHAPTER_TEXT = "合成章节正文"
TITLE = "合成有声书"


@pytest.fixture
async def books_store(store: Store) -> AsyncIterator[Store]:
    """The shared partial fixture plus the module-owned book/chapter fragment."""
    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
        await owner.execute(books_migration_sql(store.schema))
        await owner.execute(grant_books_runtime_sql(store.schema, store.role))
    yield store


def service(store: Store) -> WorkbenchBooks:
    return WorkbenchBooks(PostgresBooks(store.uow))


async def project(store: Store) -> str:
    created = await WorkbenchVersions(store.uow).create_project(
        workspace_id=WORKSPACE, name="合成有声书项目", idempotency_key="project-key"
    )
    return created.id


async def rows(store: Store, table: str) -> int:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(store.schema), sql.Identifier(table)
                )
            )
        ).fetchone()
    assert row is not None
    return int(row[0])


async def test_books_and_chapters_are_scoped_replayed_and_never_duplicated(
    books_store: Store,
) -> None:
    app = service(books_store)
    parent = await project(books_store)
    book = await app.create_book(
        project_id=parent,
        workspace_id=WORKSPACE,
        title=TITLE,
        source_text=SOURCE_TEXT,
        key=BOOK_KEY,
    )
    assert (book.status, book.casting_revision, book.source_text) == ("draft", 1, SOURCE_TEXT)
    assert await app.get_book(book_id=book.id, workspace_id=WORKSPACE) == book
    # An accepted key returns the committed snapshot, not a rebuild of the request.
    assert (
        await app.create_book(
            project_id=parent,
            workspace_id=WORKSPACE,
            title=TITLE,
            source_text=SOURCE_TEXT,
            key=BOOK_KEY,
        )
        == book
    )
    with pytest.raises(IdempotencyConflictError):
        await app.create_book(
            project_id=parent,
            workspace_id=WORKSPACE,
            title="另一个标题",
            source_text=None,
            key=BOOK_KEY,
        )
    assert await rows(books_store, "books") == 1
    assert await rows(books_store, "book_receipts") == 1

    chapter = await app.create_chapter(
        book_id=book.id,
        workspace_id=WORKSPACE,
        title="第一章",
        ordinal=0,
        text=CHAPTER_TEXT,
        key=CHAPTER_KEY,
    )
    assert (chapter.text, chapter.text_revision, chapter.ordinal) == (CHAPTER_TEXT, 1, 0)
    assert await app.get_chapter(chapter_id=chapter.id, workspace_id=WORKSPACE) == chapter
    assert (
        await app.create_chapter(
            book_id=book.id,
            workspace_id=WORKSPACE,
            title="第一章",
            ordinal=0,
            text=CHAPTER_TEXT,
            key=CHAPTER_KEY,
        )
        == chapter
    )
    with pytest.raises(OrdinalConflictError):
        await app.create_chapter(
            book_id=book.id,
            workspace_id=WORKSPACE,
            title="重复位置",
            ordinal=0,
            text=CHAPTER_TEXT,
        )
    # The same key is scoped to the resource that accepted it, not to the workspace.
    other = await app.create_book(project_id=parent, workspace_id=WORKSPACE, title="第二本合成书")
    second = await app.create_chapter(
        book_id=other.id,
        workspace_id=WORKSPACE,
        title="第一章",
        ordinal=0,
        text=CHAPTER_TEXT,
        key=CHAPTER_KEY,
    )
    assert second.id != chapter.id
    # Another workspace, a missing project and a missing book are all plain absence.
    for coroutine in (
        app.get_book(book_id=book.id, workspace_id="workspace-other"),
        app.get_chapter(chapter_id=chapter.id, workspace_id="workspace-other"),
        app.create_book(project_id="project-missing-0000", workspace_id=WORKSPACE, title=TITLE),
        app.get_book(book_id="book-missing-0000", workspace_id=WORKSPACE),
        app.get_chapter(chapter_id="chapter-missing-0000", workspace_id=WORKSPACE),
        app.create_chapter(
            book_id="book-missing-0000",
            workspace_id=WORKSPACE,
            title="第一章",
            ordinal=0,
            text=CHAPTER_TEXT,
        ),
    ):
        with pytest.raises(ResourceNotFoundError):
            await coroutine
    assert await rows(books_store, "books") == 2
    assert await rows(books_store, "chapters") == 2
    assert await rows(books_store, "book_receipts") == 3


async def test_unsupported_source_text_never_reaches_a_stored_row(books_store: Store) -> None:
    app = service(books_store)
    parent = await project(books_store)

    # A NUL byte cannot be stored, so it must fail as a request before any insert rather
    # than surfacing later as a storage failure.
    with pytest.raises(InvalidVersionChangeError):
        await app.create_book(
            project_id=parent,
            workspace_id=WORKSPACE,
            title=TITLE,
            source_text="a\x00b",
        )
    assert await rows(books_store, "books") == 0


async def test_concurrent_same_position_creates_exactly_one_chapter(books_store: Store) -> None:
    app = service(books_store)
    parent = await project(books_store)
    book = await app.create_book(project_id=parent, workspace_id=WORKSPACE, title=TITLE)

    async def create(index: int) -> Any:
        try:
            return await app.create_chapter(
                book_id=book.id,
                workspace_id=WORKSPACE,
                title=f"第{index}章",
                ordinal=0,
                text=CHAPTER_TEXT,
            )
        except OrdinalConflictError:
            return "conflict"

    results = await asyncio.gather(*(create(index) for index in range(4)))
    assert len([value for value in results if value == "conflict"]) == 3
    assert await rows(books_store, "chapters") == 1


async def test_tombstoned_project_hides_books_and_deletion_cascades_them(
    books_store: Store, cleanup: Cleanup
) -> None:
    app = service(books_store)
    parent, _ = await content(books_store)
    workspace = parent.workspace_id
    book = await app.create_book(
        project_id=parent.id, workspace_id=workspace, title=TITLE, source_text=SOURCE_TEXT
    )
    chapter = await app.create_chapter(
        book_id=book.id,
        workspace_id=workspace,
        title="第一章",
        ordinal=0,
        text=CHAPTER_TEXT,
    )
    assert await rows(books_store, "books") == 1

    # An accepted cascade plan tombstones the project before its rows are erased.
    deletions = deletions_service(books_store)
    plan = await deletions.preview(
        workspace_id=workspace, target=target(parent), mode="cascade", key="plan-key"
    )
    record = await deletions.accept(
        workspace_id=workspace,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-key",
    )
    for coroutine in (
        app.get_book(book_id=book.id, workspace_id=workspace),
        app.get_chapter(chapter_id=chapter.id, workspace_id=workspace),
        app.create_book(project_id=parent.id, workspace_id=workspace, title="新书"),
        app.create_chapter(
            book_id=book.id,
            workspace_id=workspace,
            title="第二章",
            ordinal=1,
            text=CHAPTER_TEXT,
        ),
    ):
        with pytest.raises(ResourceNotFoundError):
            await coroutine

    worker = PostgresDeletionCleanup(
        PostgresVersionUnitOfWork(cleanup.dsn, schema=books_store.schema)
    )
    assert await worker.run(record.id) is not None
    assert await rows(books_store, "books") == 0
    assert await rows(books_store, "chapters") == 0
    assert await rows(books_store, "book_receipts") == 0


async def test_listing_books_pages_newest_first_and_never_leaves_its_scope(
    books_store: Store, cleanup: Cleanup
) -> None:
    store = service(books_store)
    parent = await WorkbenchVersions(books_store.uow).create_project(
        workspace_id=WORKSPACE, name="合成有声书项目", idempotency_key="listing-project-key"
    )
    created = [
        await store.create_book(
            project_id=parent.id,
            workspace_id=WORKSPACE,
            title=f"合成有声书 {index}",
            key=f"listing-book-key-{index}",
        )
        for index in range(3)
    ]

    first = await store.list_books(project_id=parent.id, workspace_id=WORKSPACE, limit=2)
    assert [item.id for item in first.items] == [created[2].id, created[1].id]
    assert first.next_position is not None and first.next_position.kind == "books"
    second = await store.list_books(
        project_id=parent.id, workspace_id=WORKSPACE, limit=2, after=first.next_position
    )
    assert [item.id for item in second.items] == [created[0].id]
    assert second.next_position is None
    # The projection keeps the book's own facts, not just its identity.
    listed = second.items[0]
    assert listed.project_id == parent.id
    assert (listed.title, listed.casting_revision) == (
        created[0].title,
        created[0].casting_revision,
    )

    # Another workspace and an absent project are invisible rather than empty.
    with pytest.raises(ResourceNotFoundError):
        await store.list_books(project_id=parent.id, workspace_id="workspace-other", limit=10)
    with pytest.raises(ResourceNotFoundError):
        await store.list_books(project_id="project-absent", workspace_id=WORKSPACE, limit=10)

    # Once the project is tombstoned by an accepted cascade, listing hides it too.
    deletions = deletions_service(books_store)
    plan = await deletions.preview(
        workspace_id=WORKSPACE, target=target(parent), mode="cascade", key="listing-plan-key"
    )
    await deletions.accept(
        workspace_id=WORKSPACE,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="listing-delete-key",
    )
    with pytest.raises(ResourceNotFoundError):
        await store.list_books(project_id=parent.id, workspace_id=WORKSPACE, limit=10)


async def test_an_empty_project_is_an_empty_page_not_a_missing_one(books_store: Store) -> None:
    store = service(books_store)
    empty = await WorkbenchVersions(books_store.uow).create_project(
        workspace_id=WORKSPACE, name="空项目", idempotency_key="listing-empty-project-key"
    )
    page = await store.list_books(project_id=empty.id, workspace_id=WORKSPACE, limit=10)
    assert page.items == () and page.next_position is None


async def test_listing_chapters_pages_newest_first_and_respects_its_book(
    books_store: Store, cleanup: Cleanup
) -> None:
    store = service(books_store)
    parent = await WorkbenchVersions(books_store.uow).create_project(
        workspace_id=WORKSPACE, name="章节列表项目", idempotency_key="chapter-list-project-key"
    )
    book = await store.create_book(
        project_id=parent.id, workspace_id=WORKSPACE, title=TITLE, key="chapter-list-book-key"
    )
    created = [
        await store.create_chapter(
            book_id=book.id,
            workspace_id=WORKSPACE,
            title=f"第{index}章",
            ordinal=index,
            text=CHAPTER_TEXT,
            key=f"chapter-list-key-{index}",
        )
        for index in range(3)
    ]

    first = await store.list_chapters(book_id=book.id, workspace_id=WORKSPACE, limit=2)
    assert [item.id for item in first.items] == [created[2].id, created[1].id]
    assert first.next_position is not None and first.next_position.kind == "chapters"
    second = await store.list_chapters(
        book_id=book.id, workspace_id=WORKSPACE, limit=2, after=first.next_position
    )
    assert [item.id for item in second.items] == [created[0].id]
    assert second.next_position is None
    # Listing keeps the same metadata-only view as a single chapter read.
    listed = second.items[0]
    assert (listed.book_id, listed.ordinal, listed.text_revision) == (
        book.id,
        0,
        created[0].text_revision,
    )

    with pytest.raises(ResourceNotFoundError):
        await store.list_chapters(book_id=book.id, workspace_id="workspace-other", limit=10)
    with pytest.raises(ResourceNotFoundError):
        await store.list_chapters(book_id="book-absent", workspace_id=WORKSPACE, limit=10)

    empty_book = await store.create_book(
        project_id=parent.id, workspace_id=WORKSPACE, title="空书", key="chapter-list-empty-key"
    )
    empty = await store.list_chapters(book_id=empty_book.id, workspace_id=WORKSPACE, limit=10)
    assert empty.items == () and empty.next_position is None

    # Deleting the parent project hides its chapters from listing as well.
    deletions = deletions_service(books_store)
    plan = await deletions.preview(
        workspace_id=WORKSPACE, target=target(parent), mode="cascade", key="chapter-list-plan-key"
    )
    await deletions.accept(
        workspace_id=WORKSPACE,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="chapter-list-delete-key",
    )
    with pytest.raises(ResourceNotFoundError):
        await store.list_chapters(book_id=book.id, workspace_id=WORKSPACE, limit=10)


async def test_a_restored_project_stays_invisible_while_the_ledger_records_erasure(
    books_store: Store, cleanup: Cleanup
) -> None:
    """Recovery drill: restored rows must never re-expose erased workbench material.

    `docs/architecture/data-deletion.md` requires replaying the deletion ledger after a
    snapshot restore and keeping uncertain material invisible. Here the rows come back
    and the tombstones do not; only the ledger can still prove the erasure.
    """
    store = books_store
    app = service(store)
    parent, _ = await content(store)
    workspace = parent.workspace_id
    book = await app.create_book(project_id=parent.id, workspace_id=workspace, title=TITLE)
    chapter = await app.create_chapter(
        book_id=book.id, workspace_id=workspace, title="第一章", ordinal=0, text=CHAPTER_TEXT
    )
    await store.app.create_manual_transcript(
        project_id=parent.id,
        display_name="合成稿件",
        text="必须清理的合成正文",
        idempotency_key="drill-transcript",
    )
    captured = await snapshot_rows(store, "projects", "project_versions", "books", "chapters")

    deletions = deletions_service(store)
    plan = await deletions.preview(
        workspace_id=workspace, target=target(parent), mode="cascade", key="drill-plan-key"
    )
    record = await deletions.accept(
        workspace_id=workspace,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="drill-delete-key",
    )
    worker = PostgresDeletionCleanup(PostgresVersionUnitOfWork(cleanup.dsn, schema=store.schema))
    assert await worker.run(record.id) is not None
    assert await rows(store, "books") == 0

    # The restore brings the rows back without the tombstones that followed them.
    await restore_rows(store, captured)
    assert record.id in await drop_tombstones(store)
    assert record.id in await ledger_entries(store)
    assert await rows(store, "books") == 1

    versions = WorkbenchVersions(store.uow)
    for coroutine in (
        versions.get_project(parent.id),
        app.get_book(book_id=book.id, workspace_id=workspace),
        app.get_chapter(chapter_id=chapter.id, workspace_id=workspace),
        app.list_books(project_id=parent.id, workspace_id=workspace),
        app.list_chapters(book_id=book.id, workspace_id=workspace),
        store.app.list_transcripts(project_id=parent.id),
    ):
        with pytest.raises(ResourceNotFoundError):
            await coroutine
    listed = await versions.list_projects(workspace_id=workspace)
    assert parent.id not in {item.id for item in listed.items}
