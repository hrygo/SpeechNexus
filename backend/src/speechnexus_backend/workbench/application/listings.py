"""Internal seek positions and bounded pages, never public or authenticated cursors."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from speechnexus_backend.workbench.domain.books import Book, Chapter
from speechnexus_backend.workbench.domain.listings import TranscriptSummary
from speechnexus_backend.workbench.domain.render_units import RenderUnitSummary
from speechnexus_backend.workbench.domain.versions import Project

type ListingKind = Literal["projects", "transcripts", "books", "chapters", "render_units"]


class InvalidListingError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid workbench listing")


@dataclass(frozen=True, slots=True)
class ListingPosition:
    kind: ListingKind
    parent_id: str
    created_at: datetime
    item_id: str


@dataclass(frozen=True, slots=True)
class ListingPage[T]:
    items: tuple[T, ...]
    next_position: ListingPosition | None


def validate_listing(
    kind: ListingKind, parent_id: str, limit: int, after: ListingPosition | None
) -> None:
    if type(limit) is not int or not 1 <= limit <= 100 or not 1 <= len(parent_id) <= 128:
        raise InvalidListingError
    if after is not None and (
        after.kind != kind
        or after.parent_id != parent_id
        or after.created_at.utcoffset() is None
        or not 1 <= len(after.item_id) <= 128
    ):
        raise InvalidListingError


def listing_page[T: (Project, TranscriptSummary, Book, Chapter, RenderUnitSummary)](
    rows: tuple[T, ...], *, kind: ListingKind, parent_id: str, limit: int
) -> ListingPage[T]:
    items = rows[:limit]
    position = None
    if len(rows) > limit:
        last = items[-1]
        position = ListingPosition(kind, parent_id, last.created_at, last.id)
    return ListingPage(items, position)
