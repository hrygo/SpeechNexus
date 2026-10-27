"""Bounded PostgreSQL publication registry; publication is one insert, never an edit."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.binding_publication import (
    BindingPublicationConflictError,
    BindingRevisionNotFoundError,
)
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationKind,
    PublicationUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publications_sql import (
    append_publication,
    find_binding_revision,
    find_publication,
    list_publications,
)


class _Cursor:
    def __init__(self, cursor: psycopg.Cursor[dict[str, Any]], deadline: float) -> None:
        self.raw, self.deadline = cursor, deadline

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PublicationUnavailableError
        self.raw.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self.raw.execute(query, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.raw.fetchall()


class PostgresPublications:
    """Implements the publication store port against the append-only table."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Publication transactions require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def _run[T](
        self,
        operation: Callable[[_Cursor], T],
        *,
        read_only: bool = False,
        repeatable_read: bool = False,
        preserve: tuple[type[BaseException], ...] = (),
    ) -> T:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                if read_only:
                    transaction = "READ ONLY"
                    if repeatable_read:
                        transaction += ", ISOLATION LEVEL REPEATABLE READ"
                    connection.execute("SET TRANSACTION " + transaction)
                elif repeatable_read:
                    raise ValueError("Repeatable read is only valid for read operations")
                with connection.cursor() as raw:
                    result = operation(_Cursor(raw, deadline))
                    if time.monotonic() >= deadline:
                        raise PublicationUnavailableError
            if time.monotonic() >= deadline:
                raise PublicationUnavailableError
            return result
        except preserve:
            raise
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            PublicationUnavailableError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            raise PublicationUnavailableError from None

    def append(self, publication: Publication) -> bool:
        return self._run(lambda cursor: append_publication(cursor, publication))

    def publish_binding_revision(self, publication: Publication) -> tuple[Publication, bool]:
        """Validate the stored triple and append it inside one transaction."""

        def operation(cursor: _Cursor) -> tuple[Publication, bool]:
            existing = find_publication(
                cursor, publication.kind, publication.object_id, publication.revision
            )
            if existing is not None and existing.revision_id != publication.revision_id:
                raise BindingPublicationConflictError(
                    publication.object_id,
                    publication.revision,
                    existing.revision_id,
                    publication.revision_id,
                )
            stored_revision_id = find_binding_revision(
                cursor, publication.object_id, publication.revision
            )
            if stored_revision_id != publication.revision_id:
                raise BindingRevisionNotFoundError(
                    publication.object_id,
                    publication.revision,
                    publication.revision_id,
                    stored_revision_id=stored_revision_id,
                )
            if existing is not None:
                return existing, False
            if append_publication(cursor, publication):
                return publication, True
            # A concurrent writer inserted this revision first; report the stored fact.
            stored = find_publication(
                cursor, publication.kind, publication.object_id, publication.revision
            )
            if stored is None:
                raise PublicationUnavailableError
            if stored.revision_id != publication.revision_id:
                raise BindingPublicationConflictError(
                    publication.object_id,
                    publication.revision,
                    stored.revision_id,
                    publication.revision_id,
                )
            return stored, False

        return self._run(
            operation,
            preserve=(BindingRevisionNotFoundError, BindingPublicationConflictError),
        )

    def get(self, kind: PublicationKind, object_id: str, revision: int) -> Publication | None:
        return self._run(
            lambda cursor: find_publication(cursor, kind, object_id, revision), read_only=True
        )

    def list_kind(self, kind: PublicationKind) -> list[Publication]:
        return self._run(lambda cursor: list_publications(cursor, kind), read_only=True)
