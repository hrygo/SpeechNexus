"""Public model catalogue over the published bindings; no draft is ever listed."""

from __future__ import annotations

import json
from typing import Any

from speechnexus_backend.speech.application.model_catalog import (
    ModelCatalogueSnapshot,
    ReadinessFacts,
    capability_model,
    model_document,
)
from speechnexus_backend.speech.domain.publications import Publication, PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.model_catalog_sql import read_catalog_snapshot
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

    def snapshot(self) -> ModelCatalogueSnapshot:
        rows, state = self._driver._run(read_catalog_snapshot, read_only=True, repeatable_read=True)
        models: list[dict[str, Any]] = []
        capability_models: list[dict[str, Any]] = []
        for row in rows:
            definition = _definition(row["definition_json"])
            publication = Publication(
                kind=row["kind"],
                object_id=row["object_id"],
                revision=int(row["revision"]),
                revision_id=row["revision_id"],
                published_at_us=int(row["published_at_us"]),
            )
            model = model_document(
                publication, definition, owned_by=self._owned_by, readiness=self._readiness
            )
            models.append(model)
            capability_models.append(
                capability_model(
                    publication,
                    definition,
                    owned_by=self._owned_by,
                    readiness=self._readiness,
                )
            )
        return ModelCatalogueSnapshot(
            models=tuple(models),
            capability_models=tuple(capability_models),
            catalog_revision=int(state["catalog_revision"]),
            generated_at_us=int(state["generated_at_us"]),
        )

    def list_models(self) -> list[dict[str, Any]]:
        return list(self.snapshot().models)


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
