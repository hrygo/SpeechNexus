"""Strict public projection of a book or chapter; private bodies stay internal."""

import speechnexus_contracts.workbench_models as wire
from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.domain.books import Book, Chapter
from speechnexus_backend.workbench.interfaces.version_views import _time


def book_payload(value: Book) -> wire.Book:
    if type(value) is not Book:
        raise ValueError("invalid workbench book snapshot")
    return parse(
        "Book",
        {
            "object": "nexus.book",
            "id": value.id,
            "project_id": value.project_id,
            "title": value.title,
            "status": value.status,
            "casting_revision": str(value.casting_revision),
            "created_at": _time(value.created_at),
            "updated_at": _time(value.updated_at),
        },
    )


def chapter_payload(value: Chapter) -> wire.Chapter:
    if type(value) is not Chapter:
        raise ValueError("invalid workbench chapter snapshot")
    return parse(
        "Chapter",
        {
            "object": "nexus.chapter",
            "id": value.id,
            "book_id": value.book_id,
            "title": value.title,
            "ordinal": value.ordinal,
            "text_revision": str(value.text_revision),
            "created_at": _time(value.created_at),
        },
    )
