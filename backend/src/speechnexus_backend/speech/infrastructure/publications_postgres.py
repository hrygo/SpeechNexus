"""Bounded PostgreSQL publication registry; publication is one insert, never an edit."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.binding_documents import decode_document
from speechnexus_backend.speech.application.binding_publication import (
    BindingPublicationConflictError,
    BindingRevisionNotFoundError,
)
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingCapacityManifestMismatchError,
    BindingEvidenceConflictError,
    BindingPublicationEvidence,
)
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationKind,
    PublicationUnavailableError,
)
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_sql import (
    find_binding_revision_document,
    find_evidence,
    insert_evidence,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publications_sql import (
    append_publication,
    find_publication,
    list_publications,
    retire_publication,
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

    def retire(
        self,
        publication: Publication,
        *,
        retired_at_us: int,
        mode: str = "retire",
        reason: str | None = None,
    ) -> bool:
        """Hide one current revision; repeating the same retirement is a no-op."""
        return self._run(
            lambda cursor: retire_publication(
                cursor, publication, retired_at_us, mode=mode, reason=reason
            ),
            preserve=(ValueError,),
        )

    def publish_binding_revision(
        self,
        publication: Publication,
        evidence: BindingPublicationEvidence,
    ) -> tuple[Publication, BindingPublicationEvidence, bool]:
        """Validate the stored triple and capacity evidence, then write both rows."""

        def operation(cursor: _Cursor) -> tuple[Publication, BindingPublicationEvidence, bool]:
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
            stored = find_binding_revision_document(
                cursor, publication.object_id, publication.revision
            )
            if stored is None or stored[0] != publication.revision_id:
                raise BindingRevisionNotFoundError(
                    publication.object_id,
                    publication.revision,
                    publication.revision_id,
                    stored_revision_id=stored[0] if stored is not None else None,
                )
            definition = decode_document(stored[1].encode("utf-8"))
            recipe = definition.get("execution_recipe")
            declared = recipe.get("capacity_manifest_id") if isinstance(recipe, dict) else None
            if not isinstance(declared, str) or declared != evidence.capacity_manifest_id:
                raise BindingCapacityManifestMismatchError(
                    publication.object_id,
                    publication.revision,
                    declared if isinstance(declared, str) else "missing",
                    evidence.capacity_manifest_id,
                )

            existing_evidence = find_evidence(cursor, publication.object_id, publication.revision)
            if existing_evidence is not None and not existing_evidence.same_facts(evidence):
                raise BindingEvidenceConflictError(
                    f"binding_id={publication.object_id} revision={publication.revision} "
                    "already records different publication evidence"
                )
            if existing_evidence is not None:
                if existing is None:
                    # The evidence foreign key makes this unreachable in intact
                    # storage; never invent a publication to match it.
                    raise PublicationUnavailableError
                return existing, existing_evidence, False

            if existing is not None:
                if not insert_evidence(cursor, evidence):
                    stored_evidence = find_evidence(
                        cursor, publication.object_id, publication.revision
                    )
                    if stored_evidence is None or not stored_evidence.same_facts(evidence):
                        raise BindingEvidenceConflictError(
                            f"binding_id={publication.object_id} "
                            f"revision={publication.revision} records conflicting evidence"
                        )
                    return existing, stored_evidence, False
                return existing, evidence, False

            if append_publication(cursor, publication):
                if not insert_evidence(cursor, evidence):
                    raise PublicationUnavailableError
                return publication, evidence, True

            # A concurrent writer inserted this revision first; report the stored fact.
            stored_publication = find_publication(
                cursor, publication.kind, publication.object_id, publication.revision
            )
            if stored_publication is None:
                raise PublicationUnavailableError
            if stored_publication.revision_id != publication.revision_id:
                raise BindingPublicationConflictError(
                    publication.object_id,
                    publication.revision,
                    stored_publication.revision_id,
                    publication.revision_id,
                )
            stored_evidence = find_evidence(cursor, publication.object_id, publication.revision)
            if stored_evidence is None:
                if not insert_evidence(cursor, evidence):
                    raise PublicationUnavailableError
                stored_evidence = evidence
            elif not stored_evidence.same_facts(evidence):
                raise BindingEvidenceConflictError(
                    f"binding_id={publication.object_id} revision={publication.revision} "
                    "records conflicting evidence"
                )
            return stored_publication, stored_evidence, False

        return self._run(
            operation,
            preserve=(
                BindingRevisionNotFoundError,
                BindingPublicationConflictError,
                BindingCapacityManifestMismatchError,
                BindingEvidenceConflictError,
            ),
        )

    def get(self, kind: PublicationKind, object_id: str, revision: int) -> Publication | None:
        return self._run(
            lambda cursor: find_publication(cursor, kind, object_id, revision), read_only=True
        )

    def list_kind(self, kind: PublicationKind) -> list[Publication]:
        return self._run(lambda cursor: list_publications(cursor, kind), read_only=True)
