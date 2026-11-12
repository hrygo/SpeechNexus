"""Bounded administrative reads of recorded runtime instances; no storage imports.

The page is accepted only when storage returned a strictly ascending run on the
``(instance_id, generation)`` keyset key, every row satisfies the requested
filters, and at most one row beyond the limit was over-read so ``has_more`` is
derivable. A page that is not strictly ascending means the ordering guarantee in
the statement was not actually applied; it is refused instead of being silently
re-sorted here.
"""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.speech.domain.assets import counter, identifier
from speechnexus_backend.speech.domain.model_instances import (
    InstancePosition,
    ModelInstance,
    ModelInstanceLookup,
    ModelInstancePage,
    ModelInstanceQuery,
    ModelInstanceUnavailableError,
)


class ModelInstanceReads(Protocol):
    """Storage port; implementations own connections and deadlines."""

    def scan(self, query: ModelInstanceQuery) -> tuple[ModelInstance, ...]: ...

    def find(self, instance_id: str, generation: int) -> ModelInstanceLookup: ...


class ModelInstanceService:
    def __init__(self, reads: ModelInstanceReads) -> None:
        self._reads = reads

    def list(self, query: ModelInstanceQuery) -> ModelInstancePage:
        if not isinstance(query, ModelInstanceQuery):
            raise ModelInstanceUnavailableError
        try:
            rows = self._reads.scan(query)
            if not isinstance(rows, tuple) or len(rows) > query.limit + 1:
                raise ModelInstanceUnavailableError
            previous: InstancePosition | None = None
            for row in rows:
                if not isinstance(row, ModelInstance) or not query.matches(row):
                    raise ModelInstanceUnavailableError
                if previous is not None and row.position <= previous:
                    raise ModelInstanceUnavailableError
                previous = row.position
        except ModelInstanceUnavailableError:
            raise
        except ValueError, TypeError, KeyError:
            raise ModelInstanceUnavailableError from None
        items = rows[: query.limit]
        return ModelInstancePage(
            items,
            items[-1].position if len(rows) > query.limit else None,
        )

    def get(self, instance_id: str, generation: int) -> ModelInstanceLookup:
        try:
            identifier(instance_id)
            counter(generation, minimum=1)
            lookup = self._reads.find(instance_id, generation)
            if not isinstance(lookup, ModelInstanceLookup):
                raise ModelInstanceUnavailableError
            requested = InstancePosition(instance_id, generation)
            if lookup.instance is not None and lookup.instance.position != requested:
                # A stored row for another generation cannot answer this lookup;
                # reporting it would return a different instance generation.
                raise ModelInstanceUnavailableError
        except ModelInstanceUnavailableError:
            raise
        except ValueError, TypeError, KeyError:
            raise ModelInstanceUnavailableError from None
        return lookup
