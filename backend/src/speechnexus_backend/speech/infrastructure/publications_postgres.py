"""Bounded PostgreSQL publication registry; publication is one insert, never an edit."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationKind,
    PublicationUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publications_sql import (
    append_publication,
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

    def _run[T](self, operation: Callable[[_Cursor], T], *, read_only: bool = False) -> T:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                if read_only:
                    connection.execute("SET TRANSACTION READ ONLY")
                with connection.cursor() as raw:
                    result = operation(_Cursor(raw, deadline))
                    if time.monotonic() >= deadline:
                        raise PublicationUnavailableError
            if time.monotonic() >= deadline:
                raise PublicationUnavailableError
            return result
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

    def get(self, kind: PublicationKind, object_id: str, revision: int) -> Publication | None:
        return self._run(
            lambda cursor: find_publication(cursor, kind, object_id, revision), read_only=True
        )

    def list_kind(self, kind: PublicationKind) -> list[Publication]:
        return self._run(lambda cursor: list_publications(cursor, kind), read_only=True)
