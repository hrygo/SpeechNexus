"""Read-only admin projection: draft content plus publication evidence.

This adapter never writes and never repairs missing evidence. A publication
without matching evidence is reported as unavailable so the admin GET cannot
claim either ``draft`` or ``published`` when neither is proven.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.binding_documents import decode_document
from speechnexus_backend.speech.application.binding_publication import PublishedBinding
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.binding_drafts import (
    BindingDraft,
    BindingUnavailableError,
)
from speechnexus_backend.speech.domain.configuration import revision_number
from speechnexus_backend.speech.infrastructure.binding_drafts_sql import read_draft
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_sql import (
    find_evidence,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publications_sql import find_publication


class _Cursor:
    def __init__(self, cursor: psycopg.Cursor[dict[str, Any]], deadline: float) -> None:
        self.raw, self.deadline = cursor, deadline

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BindingUnavailableError
        self.raw.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self.raw.execute(query, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.raw.fetchall()


class PostgresBindingAdminReader:
    """Implements the admin GET ports over one bounded read-only pool."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Binding admin reads require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def _run[T](self, operation: Callable[[_Cursor], T]) -> T:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                with connection.cursor() as raw:
                    result = operation(_Cursor(raw, deadline))
                    if time.monotonic() >= deadline:
                        raise BindingUnavailableError
            if time.monotonic() >= deadline:
                raise BindingUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            BindingUnavailableError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            raise BindingUnavailableError from None

    def get(self, binding_id: str, revision: str) -> BindingDraft | None:
        identifier(binding_id)
        revision_number(revision)
        return self._run(lambda cursor: read_draft(cursor, binding_id, revision))

    def project(self, draft: BindingDraft) -> PublishedBinding | None:
        """Return published facts, ``None`` for an unpublicized draft, or fail closed."""

        def operation(cursor: _Cursor) -> PublishedBinding | None:
            revision = int(draft.revision)
            publication = find_publication(cursor, "binding", draft.binding_id, revision)
            if publication is None:
                return None
            evidence = find_evidence(cursor, draft.binding_id, revision)
            if evidence is None:
                raise BindingUnavailableError
            if (
                publication.revision_id != draft.revision_id
                or evidence.revision_id != draft.revision_id
            ):
                raise BindingUnavailableError
            definition = decode_document(draft.definition)
            recipe = definition.get("execution_recipe")
            declared = recipe.get("capacity_manifest_id") if isinstance(recipe, dict) else None
            if not isinstance(declared, str) or declared != evidence.capacity_manifest_id:
                raise BindingUnavailableError
            return PublishedBinding(
                published_at_us=publication.published_at_us,
                capacity_manifest_id=evidence.capacity_manifest_id,
                qualification_report_id=evidence.qualification_report_id,
            )

        return self._run(operation)
