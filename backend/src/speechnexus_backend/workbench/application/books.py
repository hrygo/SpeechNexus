"""Book and chapter application boundary; the injected store owns each transaction."""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.workbench.application.listings import (
    ListingPage,
    ListingPosition,
    listing_page,
    validate_listing,
)
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.domain.books import (
    Book,
    Chapter,
    validate_book_request,
    validate_chapter_request,
)
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError


class OrdinalConflictError(Exception):
    """The requested chapter position is already used inside this book."""

    def __init__(self) -> None:
        super().__init__("chapter ordinal already used")


class BookStore(Protocol):
    """Short, keyed transactions; a replay returns the row accepted for that key."""

    async def create_book(
        self,
        *,
        project_id: str,
        workspace_id: str,
        title: str,
        source_text: str | None,
        key: str | None,
    ) -> Book: ...

    async def get_book(self, *, book_id: str, workspace_id: str) -> Book: ...

    async def list_books(
        self, *, project_id: str, workspace_id: str, limit: int, after: ListingPosition | None
    ) -> tuple[Book, ...] | None:
        """None for an absent project; parent and rows share one statement view."""
        ...

    async def create_chapter(
        self,
        *,
        book_id: str,
        workspace_id: str,
        title: str,
        ordinal: int,
        text: str,
        key: str | None,
    ) -> Chapter: ...

    async def get_chapter(self, *, chapter_id: str, workspace_id: str) -> Chapter: ...

    async def list_chapters(
        self, *, book_id: str, workspace_id: str, limit: int, after: ListingPosition | None
    ) -> tuple[Chapter, ...] | None:
        """None for an absent book; book visibility and rows share one statement view."""
        ...


def _key(key: str | None) -> str | None:
    if key is None:
        return None
    if not 8 <= len(key) <= 128:
        raise InvalidVersionChangeError("invalid workbench request key")
    return key


class WorkbenchBooks:
    """Typed commands; transport parsing, cursors and receipts stay outside."""

    def __init__(self, store: BookStore) -> None:
        self._store = store

    async def create_book(
        self,
        *,
        project_id: str,
        workspace_id: str,
        title: str,
        source_text: str | None = None,
        key: str | None = None,
    ) -> Book:
        validate_book_request(title=title, source_text=source_text)
        return await self._store.create_book(
            project_id=project_id,
            workspace_id=workspace_id,
            title=title,
            source_text=source_text,
            key=_key(key),
        )

    async def get_book(self, *, book_id: str, workspace_id: str) -> Book:
        return await self._store.get_book(book_id=book_id, workspace_id=workspace_id)

    async def list_books(
        self,
        *,
        project_id: str,
        workspace_id: str,
        limit: int = 50,
        after: ListingPosition | None = None,
    ) -> ListingPage[Book]:
        validate_listing("books", project_id, limit, after)
        rows = await self._store.list_books(
            project_id=project_id, workspace_id=workspace_id, limit=limit + 1, after=after
        )
        if rows is None:
            raise ResourceNotFoundError
        return listing_page(rows, kind="books", parent_id=project_id, limit=limit)

    async def create_chapter(
        self,
        *,
        book_id: str,
        workspace_id: str,
        title: str,
        ordinal: int,
        text: str,
        key: str | None = None,
    ) -> Chapter:
        validate_chapter_request(title=title, ordinal=ordinal, text=text)
        return await self._store.create_chapter(
            book_id=book_id,
            workspace_id=workspace_id,
            title=title,
            ordinal=ordinal,
            text=text,
            key=_key(key),
        )

    async def get_chapter(self, *, chapter_id: str, workspace_id: str) -> Chapter:
        return await self._store.get_chapter(chapter_id=chapter_id, workspace_id=workspace_id)

    async def list_chapters(
        self,
        *,
        book_id: str,
        workspace_id: str,
        limit: int = 50,
        after: ListingPosition | None = None,
    ) -> ListingPage[Chapter]:
        validate_listing("chapters", book_id, limit, after)
        rows = await self._store.list_chapters(
            book_id=book_id, workspace_id=workspace_id, limit=limit + 1, after=after
        )
        if rows is None:
            raise ResourceNotFoundError
        return listing_page(rows, kind="chapters", parent_id=book_id, limit=limit)
