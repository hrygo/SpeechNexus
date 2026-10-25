"""Generated book and chapter HTTP boundaries with no database, object store or network."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.books import (
    OrdinalConflictError,
    WorkbenchBooks,
)
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.books import Book, Chapter
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError
from speechnexus_backend.workbench.interfaces.cursors import CursorCodec
from speechnexus_backend.workbench.interfaces.http_books import create_book_router

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
PROJECT = "project-demo"
BOOK = "book-synthetic-one"
CHAPTER = "chapter-synthetic-one"
TITLE = "合成有声书"
TEXT = "合成章节正文"
BOOKS_PATH = f"/v1/workbench/projects/{PROJECT}/books"
BOOK_PATH = f"/v1/workbench/books/{BOOK}"
CHAPTERS_PATH = f"{BOOK_PATH}/chapters"
CHAPTER_PATH = f"/v1/workbench/chapters/{CHAPTER}"
BOOK_KEY = "book-request-key-0001"
CHAPTER_KEY = "chapter-request-key-0001"


def book(**changes: Any) -> Book:
    values: dict[str, Any] = {
        "id": BOOK,
        "project_id": PROJECT,
        "title": TITLE,
        "status": "draft",
        "casting_revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
        "source_text": None,
    }
    values.update(changes)
    return Book(**values)


def chapter(**changes: Any) -> Chapter:
    values: dict[str, Any] = {
        "id": CHAPTER,
        "book_id": BOOK,
        "title": "第一章",
        "ordinal": 0,
        "text_revision": 1,
        "created_at": NOW,
        "text": TEXT,
    }
    values.update(changes)
    return Chapter(**values)


class Store:
    def __init__(self) -> None:
        self.books: list[dict[str, Any]] = []
        self.chapters: list[dict[str, Any]] = []
        self.book = book()
        self.chapter = chapter()
        self.failure: BaseException | None = None
        self.book_failure: BaseException | None = None
        self.listing: tuple[Book, ...] | None = ()
        self.listings: list[dict[str, Any]] = []
        self.chapter_listing: tuple[Chapter, ...] | None = ()
        self.chapter_listings: list[dict[str, Any]] = []

    async def create_book(self, **kw: Any) -> Book:
        self.books.append(kw)
        if self.book_failure is not None:
            raise self.book_failure
        if self.failure is not None:
            raise self.failure
        return self.book

    async def list_books(self, **kw: Any) -> tuple[Book, ...] | None:
        self.listings.append(kw)
        if self.failure is not None:
            raise self.failure
        rows = self.listing
        if rows is None:
            return None
        after = kw["after"]
        if after is not None:
            # Keyset semantics: return the rows strictly after the given position.
            index = next(i for i, item in enumerate(rows) if item.id == after.item_id)
            rows = rows[index + 1 :]
        return rows[: kw["limit"]]

    async def get_book(self, **kw: Any) -> Book:
        if kw["book_id"] != self.book.id:
            raise ResourceNotFoundError
        return self.book

    async def create_chapter(self, **kw: Any) -> Chapter:
        self.chapters.append(kw)
        if self.failure is not None:
            raise self.failure
        return self.chapter

    async def list_chapters(self, **kw: Any) -> tuple[Chapter, ...] | None:
        self.chapter_listings.append(kw)
        if self.failure is not None:
            raise self.failure
        rows = self.chapter_listing
        if rows is None:
            return None
        after = kw["after"]
        if after is not None:
            index = next(i for i, item in enumerate(rows) if item.id == after.item_id)
            rows = rows[index + 1 :]
        return rows[: kw["limit"]]

    async def get_chapter(self, **kw: Any) -> Chapter:
        if kw["chapter_id"] != self.chapter.id:
            raise ResourceNotFoundError
        return self.chapter


class Harness:
    def __init__(self) -> None:
        self.store = Store()

    @property
    def service(self) -> WorkbenchBooks:
        return WorkbenchBooks(self.store)


def app(harness: Harness | None) -> FastAPI:
    service = None if harness is None else harness.service
    value = create_app(
        None,
        books=service,
        default_workspace_id="workspace-demo",
        deployment_id="synthetic",
        cursor_key=b"synthetic-32-byte-cursor-key-00000",
        max_json_bytes=4096,
        body_timeout_seconds=0.02,
    )
    if harness is None:
        value.include_router(
            create_book_router(
                None,
                default_workspace_id="workspace-demo",
                cursor=CursorCodec("synthetic", b"synthetic-32-byte-cursor-key-00000"),
                max_json_bytes=4096,
                body_timeout_seconds=0.02,
            )
        )
    return value


async def call(
    harness: Harness | None,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app(harness))
    async with httpx.AsyncClient(transport=transport, base_url="http://workbench.test") as client:
        return (
            await client.post(path, json=body, headers=headers)
            if body is not None
            else await client.get(path, headers=headers)
        )


def book_body(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"title": TITLE}
    values.update(changes)
    return values


def chapter_body(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"title": "第一章", "ordinal": 0, "text": TEXT}
    values.update(changes)
    return values


async def test_create_book_returns_the_public_projection_without_the_private_body() -> None:
    harness = Harness()
    harness.store.book = book(source_text="私有书稿正文")
    response = await call(
        harness,
        BOOKS_PATH,
        body=book_body(source_text="私有书稿正文"),
        headers={"Idempotency-Key": BOOK_KEY},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["object"] == "nexus.book"
    assert body["id"] == BOOK and body["project_id"] == PROJECT
    assert body["status"] == "draft" and body["casting_revision"] == "1"
    assert "source_text" not in body
    assert "私有书稿正文" not in response.text
    # The request reaches the store unchanged, including the private body and the key.
    assert harness.store.books == [
        {
            "project_id": PROJECT,
            "workspace_id": "workspace-demo",
            "title": TITLE,
            "source_text": "私有书稿正文",
            "key": BOOK_KEY,
        }
    ]


async def test_accepted_book_key_replays_the_stored_snapshot() -> None:
    harness = Harness()
    harness.store.book = book()
    first = await call(harness, BOOKS_PATH, body=book_body(), headers={"Idempotency-Key": BOOK_KEY})
    # The store owns replay; a second call must return the same committed snapshot.
    second = await call(
        harness, BOOKS_PATH, body=book_body(), headers={"Idempotency-Key": BOOK_KEY}
    )

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert [call_["key"] for call_ in harness.store.books] == [BOOK_KEY, BOOK_KEY]


async def test_conflicting_book_key_is_reported_as_a_conflict() -> None:
    harness = Harness()
    harness.store.book_failure = IdempotencyConflictError()
    response = await call(
        harness, BOOKS_PATH, body=book_body(), headers={"Idempotency-Key": BOOK_KEY}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


async def test_absent_project_or_book_is_reported_as_absence_without_leaking_detail() -> None:
    harness = Harness()
    harness.store.book_failure = ResourceNotFoundError()
    missing_project = await call(harness, BOOKS_PATH, body=book_body())

    assert missing_project.status_code == 404
    assert missing_project.json()["error"]["code"] == "resource_not_found"
    assert PROJECT not in missing_project.text

    missing_book = await call(Harness(), "/v1/workbench/books/book-synthetic-two")
    assert missing_book.status_code == 404
    assert missing_book.json()["error"]["code"] == "resource_not_found"


async def test_get_book_returns_the_committed_snapshot() -> None:
    harness = Harness()
    response = await call(harness, BOOK_PATH)

    assert response.status_code == 200
    assert response.json()["id"] == BOOK
    assert "source_text" not in response.json()


async def test_create_chapter_reports_a_taken_ordinal_as_a_conflict() -> None:
    harness = Harness()
    harness.store.failure = OrdinalConflictError()
    response = await call(
        harness,
        CHAPTERS_PATH,
        body=chapter_body(ordinal=0),
        headers={"Idempotency-Key": CHAPTER_KEY},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ordinal_conflict"
    assert harness.store.chapters[0]["book_id"] == BOOK
    assert harness.store.chapters[0]["ordinal"] == 0


async def test_get_chapter_never_exposes_or_accepts_the_private_body() -> None:
    harness = Harness()
    found = await call(harness, CHAPTER_PATH)

    assert found.status_code == 200
    body = found.json()
    assert body["object"] == "nexus.chapter"
    assert body["ordinal"] == 0 and body["text_revision"] == "1"
    assert "text" not in body
    assert TEXT not in found.text


async def test_malformed_requests_are_rejected_before_reaching_the_store() -> None:
    harness = Harness()
    missing_title = await call(harness, BOOKS_PATH, body={})
    negative_ordinal = await call(harness, CHAPTERS_PATH, body=chapter_body(ordinal=-1))
    unknown_field = await call(harness, BOOKS_PATH, body=book_body(private_path="/etc/passwd"))

    assert missing_title.status_code == 400
    assert negative_ordinal.status_code == 400
    assert unknown_field.status_code == 400
    assert harness.store.books == [] and harness.store.chapters == []


async def test_invalid_path_identifiers_are_rejected_before_reaching_the_store() -> None:
    harness = Harness()
    book = await call(harness, "/v1/workbench/books/UPPERCASE-ID")
    chapter = await call(harness, "/v1/workbench/chapters/UPPERCASE-ID")

    assert book.status_code == 400
    assert chapter.status_code == 400
    assert harness.store.books == [] and harness.store.chapters == []


async def test_unconfigured_books_report_a_stable_unavailability() -> None:
    response = await call(None, BOOKS_PATH, body=book_body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "workbench_unavailable"


async def test_application_rejects_unsafe_requests_before_any_transaction() -> None:
    store = Store()
    service = WorkbenchBooks(store)
    with pytest.raises(InvalidVersionChangeError):
        _ = await service.create_book(project_id=PROJECT, workspace_id="w", title="", key=None)
    with pytest.raises(InvalidVersionChangeError):
        _ = await service.create_book(
            project_id=PROJECT, workspace_id="w", title=TITLE, key="short"
        )
    with pytest.raises(InvalidVersionChangeError):
        _ = await service.create_chapter(
            book_id=BOOK, workspace_id="w", title="第一章", ordinal=-1, text=TEXT
        )
    with pytest.raises(InvalidVersionChangeError):
        _ = await service.create_chapter(
            book_id=BOOK, workspace_id="w", title="第一章", ordinal=0, text=""
        )
    assert store.books == [] and store.chapters == []


async def test_unsupported_source_text_characters_are_a_client_error_not_a_storage_error() -> None:
    harness = Harness()
    response = await call(harness, BOOKS_PATH, body=book_body(source_text="a\u0000b"))

    # The row can never be stored, so this must be a request failure with the declared
    # 422 status rather than a 503 storage signal from a failed insert.
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_change"
    assert harness.store.books == []


def test_domain_rejects_inconsistent_or_unsafe_book_facts() -> None:
    with pytest.raises(ValueError):
        book(id="UPPERCASE")
    with pytest.raises(ValueError):
        book(status="published")
    with pytest.raises(ValueError):
        book(casting_revision=0)
    with pytest.raises(ValueError):
        book(updated_at=datetime(2026, 10, 24, 11, 0, tzinfo=UTC))
    with pytest.raises(ValueError):
        book(created_at=datetime(2026, 10, 24, 12, 0))
    with pytest.raises(ValueError):
        chapter(ordinal=-1)
    with pytest.raises(ValueError):
        chapter(text="")
    with pytest.raises(ValueError):
        chapter(text="bad\x00text")
    with pytest.raises(ValueError):
        chapter(book_id="BOOK")
    with pytest.raises(ValueError):
        book(source_text="bad\x00text")
    # An empty private body is a valid draft, not an unsupported value.
    assert book(source_text="").source_text == ""


async def test_listing_books_pages_by_cursor_and_hides_absent_projects() -> None:
    harness = Harness()
    harness.store.listing = (book(), book(id="book-synthetic-two", title="第二本"))
    first = await call(harness, f"{BOOKS_PATH}?limit=1")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["object"] == "nexus.list"
    assert [item["id"] for item in body["data"]] == [BOOK]
    assert body["has_more"] is True and body["next_cursor"] is not None
    # The adapter was asked for one extra row so `has_more` needs no second query.
    assert harness.store.listings[0]["limit"] == 2
    assert harness.store.listings[0]["after"] is None

    second = await call(harness, f"{BOOKS_PATH}?limit=1&cursor={body['next_cursor']}")
    assert second.status_code == 200, second.text
    assert second.json()["data"][0]["id"] == "book-synthetic-two"
    assert harness.store.listings[1]["after"] is not None
    assert harness.store.listings[1]["after"].item_id == BOOK

    harness.store.listing = None
    absent = await call(harness, BOOKS_PATH)
    assert absent.status_code == 404
    assert absent.json()["error"]["code"] == "resource_not_found"


async def test_listing_books_rejects_foreign_cursors_and_out_of_range_limits() -> None:
    harness = Harness()
    harness.store.listing = (book(), book(id="book-synthetic-two"))
    page = (await call(harness, f"{BOOKS_PATH}?limit=1")).json()
    token = str(page["next_cursor"])

    # The same token must not be replayable against another parent scope.
    foreign = await call(harness, f"/v1/workbench/projects/project-other/books?cursor={token}")
    assert foreign.status_code == 400
    assert foreign.json()["error"]["code"] == "invalid_cursor"
    assert "project-other" not in foreign.text

    for query in ("limit=0", "limit=101", "cursor=not-a-cursor"):
        refused = await call(harness, f"{BOOKS_PATH}?{query}")
        assert refused.status_code == 400, query
        assert refused.json()["error"]["code"] in {"invalid_cursor", "invalid_request"}
    # Only the accepted page reached the store: refusals happen at the boundary.
    assert [entry["limit"] for entry in harness.store.listings] == [2]


async def test_listing_chapters_pages_by_its_own_scope() -> None:
    harness = Harness()
    harness.store.chapter_listing = (chapter(), chapter(id="chapter-synthetic-two", ordinal=1))
    first = await call(harness, f"{CHAPTERS_PATH}?limit=1")
    assert first.status_code == 200, first.text
    body = first.json()
    assert [item["id"] for item in body["data"]] == [CHAPTER]
    assert body["has_more"] is True
    assert harness.store.chapter_listings[0]["limit"] == 2

    second = await call(harness, f"{CHAPTERS_PATH}?limit=1&cursor={body['next_cursor']}")
    assert second.status_code == 200, second.text
    assert second.json()["data"][0]["id"] == "chapter-synthetic-two"

    # A book cursor cannot be replayed against a different book.
    foreign = await call(
        harness, f"/v1/workbench/books/book-other/chapters?cursor={body['next_cursor']}"
    )
    assert foreign.status_code == 400
    assert foreign.json()["error"]["code"] == "invalid_cursor"
    assert "book-other" not in foreign.text

    harness.store.chapter_listing = None
    absent = await call(harness, CHAPTERS_PATH)
    assert absent.status_code == 404
    assert absent.json()["error"]["code"] == "resource_not_found"
