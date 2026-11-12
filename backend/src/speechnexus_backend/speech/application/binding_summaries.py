"""Read-only binding family listing; no allocation, publication or lifecycle write."""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.speech.domain.binding_summaries import (
    BindingSummaryPage,
    BindingSummaryQuery,
    BindingSummaryUnavailableError,
    ModelBindingSummary,
)


class BindingSummaryReads(Protocol):
    def scan(self, query: BindingSummaryQuery) -> tuple[ModelBindingSummary, ...]:
        """Return ordered rows after the keyset, bounded to limit + 1."""
        ...


class BindingSummaryService:
    """The projection is validated on read; storage never paginates silently."""

    def __init__(self, reads: BindingSummaryReads) -> None:
        self._reads = reads

    def list(self, query: BindingSummaryQuery) -> BindingSummaryPage:
        if not isinstance(query, BindingSummaryQuery):
            raise BindingSummaryUnavailableError
        rows = self._reads.scan(query)
        if not isinstance(rows, tuple) or len(rows) > query.limit + 1:
            raise BindingSummaryUnavailableError
        previous = query.after_id or ""
        for record in rows:
            if not isinstance(record, ModelBindingSummary) or record.binding_id <= previous:
                raise BindingSummaryUnavailableError
            previous = record.binding_id
            if query.status is not None and record.publication_status != query.status:
                # A filter that leaks another status cannot be trusted: the
                # predicate and the derived projection disagree.
                raise BindingSummaryUnavailableError
        items = rows[: query.limit]
        return BindingSummaryPage(items, items[-1].binding_id if len(rows) > query.limit else None)
