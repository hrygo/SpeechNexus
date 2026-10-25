"""Audiobook composition: private chapter text with a stable public projection.

The book holds its own source and chapter bodies. Those bodies never leave the
deployment network and are never echoed by a public payload: the wire projection
carries only titles, positions and revisions. Composition, casting and render
units build on this snapshot; the speech base only ever receives a bounded unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Self

from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    validate_revision_number,
)

type BookStatus = Literal["draft", "ready", "exporting"]

MAX_TITLE_LENGTH = 256
MAX_SOURCE_TEXT_LENGTH = 5_000_000
MAX_CHAPTER_TEXT_LENGTH = 5_000_000
MAX_ORDINAL = 10**19 - 1
_BOOK_STATUSES = frozenset({"draft", "ready", "exporting"})
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")


def _identifier(value: str, label: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidVersionChangeError(f"invalid {label} identifier")


def _text(value: str, *, maximum: int, label: str) -> None:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise InvalidVersionChangeError(f"{label} length outside the supported range")
    # Keep accepted content lossless across UTF-8 transport and text storage.
    if "\x00" in value:
        raise InvalidVersionChangeError(f"{label} contains an unsupported character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidVersionChangeError(f"{label} contains an unsupported character") from None


def _optional_text(value: str, *, maximum: int, label: str) -> None:
    """A private body may be empty, but it must survive UTF-8 text storage."""
    if type(value) is not str or not 0 <= len(value) <= maximum:
        raise InvalidVersionChangeError(f"{label} length outside the supported range")
    if "\x00" in value:
        raise InvalidVersionChangeError(f"{label} contains an unsupported character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidVersionChangeError(f"{label} contains an unsupported character") from None


def book_time(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise InvalidVersionChangeError("book time must be an aware datetime")
    return now


def validate_book_request(*, title: str, source_text: str | None) -> None:
    """Transport-independent request check; the caller owns parsing and keys."""
    _text(title, maximum=MAX_TITLE_LENGTH, label="book title")
    # A supplied body is stored exactly; only its absence is a separate fact.
    # A supplied body is stored exactly and may be empty; only NUL-free UTF-8 text can be
    # stored, so a client mistake must not surface later as a storage failure.
    if source_text is not None:
        _optional_text(source_text, maximum=MAX_SOURCE_TEXT_LENGTH, label="book source text")


def validate_chapter_request(*, title: str, ordinal: int, text: str) -> None:
    _text(title, maximum=MAX_TITLE_LENGTH, label="chapter title")
    if type(ordinal) is not int or not 0 <= ordinal <= MAX_ORDINAL:
        raise InvalidVersionChangeError("chapter ordinal outside the supported range")
    _text(text, maximum=MAX_CHAPTER_TEXT_LENGTH, label="chapter text")


@dataclass(frozen=True, slots=True)
class Book:
    """An immutable book snapshot; casting changes arrive as a new revision later."""

    id: str
    project_id: str
    title: str
    status: BookStatus
    casting_revision: int
    created_at: datetime
    updated_at: datetime
    source_text: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _identifier(self.id, "book")
        _identifier(self.project_id, "project")
        _text(self.title, maximum=MAX_TITLE_LENGTH, label="book title")
        if self.status not in _BOOK_STATUSES:
            raise InvalidVersionChangeError("unknown book status")
        validate_revision_number(self.casting_revision)
        created = book_time(self.created_at)
        if book_time(self.updated_at) < created:
            raise InvalidVersionChangeError("book time must be monotonic")
        if self.source_text is not None:
            _optional_text(
                self.source_text, maximum=MAX_SOURCE_TEXT_LENGTH, label="book source text"
            )

    @classmethod
    def create(
        cls,
        *,
        book_id: str,
        project_id: str,
        title: str,
        source_text: str | None,
        now: datetime,
    ) -> Self:
        return cls(book_id, project_id, title, "draft", 1, now, now, source_text)


@dataclass(frozen=True, slots=True)
class Chapter:
    """A chapter body with its own text revision; the body stays private."""

    id: str
    book_id: str
    title: str
    ordinal: int
    text_revision: int
    created_at: datetime
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        _identifier(self.id, "chapter")
        _identifier(self.book_id, "book")
        _text(self.title, maximum=MAX_TITLE_LENGTH, label="chapter title")
        if type(self.ordinal) is not int or not 0 <= self.ordinal <= MAX_ORDINAL:
            raise InvalidVersionChangeError("chapter ordinal outside the supported range")
        _text(self.text, maximum=MAX_CHAPTER_TEXT_LENGTH, label="chapter text")
        validate_revision_number(self.text_revision)
        book_time(self.created_at)

    @classmethod
    def create(
        cls,
        *,
        chapter_id: str,
        book_id: str,
        title: str,
        ordinal: int,
        text: str,
        now: datetime,
    ) -> Self:
        return cls(chapter_id, book_id, title, ordinal, 1, now, text)
