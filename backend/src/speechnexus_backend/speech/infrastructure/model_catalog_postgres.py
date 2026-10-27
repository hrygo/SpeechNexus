"""Public model catalogue over the published bindings; no draft is ever listed."""

from __future__ import annotations

import json
from typing import Any

from speechnexus_backend.speech.application.model_catalog import (
    ReadinessFacts,
    model_document,
)
from speechnexus_backend.speech.domain.publications import Publication, PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.model_catalog_sql import list_published_bindings
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications


class PostgresModelCatalog:
    """One bounded read: publications joined with their exact binding documents."""

    def __init__(
        self,
        pool: SpeechPool,
        *,
        owned_by: str = "speechnexus",
        readiness: ReadinessFacts | None = None,
        timeout_seconds: float = 3,
    ) -> None:
        if not owned_by or len(owned_by) > 128:
            raise ValueError("Model catalogue requires a bounded owner")
        self._driver = PostgresPublications(pool, timeout_seconds=timeout_seconds)
        self._owned_by = owned_by
        self._readiness = readiness or ReadinessFacts()

    def list_models(self) -> list[dict[str, Any]]:
        rows = self._driver._run(list_published_bindings, read_only=True)
        models: list[dict[str, Any]] = []
        for row in rows:
            definition = _definition(row["definition_json"])
            publication = Publication(
                kind=row["kind"],
                object_id=row["object_id"],
                revision=int(row["revision"]),
                revision_id=row["revision_id"],
                published_at_us=int(row["published_at_us"]),
            )
            models.append(
                model_document(
                    publication, definition, owned_by=self._owned_by, readiness=self._readiness
                )
            )
        return models


def _definition(raw: object) -> dict[str, Any]:
    """A published document that cannot be decoded fails the read, never silently."""
    if not isinstance(raw, str):
        raise PublicationUnavailableError
    try:
        value = json.loads(raw)
    except json.JSONDecodeError, RecursionError:
        raise PublicationUnavailableError from None
    if not isinstance(value, dict):
        raise PublicationUnavailableError
    return value
