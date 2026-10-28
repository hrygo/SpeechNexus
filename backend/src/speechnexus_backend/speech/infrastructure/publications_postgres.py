"""Bounded PostgreSQL publication registry; publication is one insert, never an edit."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.binding_documents import decode_document
from speechnexus_backend.speech.application.binding_publication import (
    MISSING_HEAD,
    BindingHeadPreconditionError,
    BindingIntentConflictError,
    BindingPublicationConflictError,
    BindingPublishIntent,
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

    def append(
        self,
        publication: Publication,
        *,
        expected_current_revision: object = MISSING_HEAD,
        expected_catalog_revision: object = MISSING_HEAD,
    ) -> bool:
        if publication.kind == "binding":
            raise BindingHeadPreconditionError("binding_publication_requires_complete_intent")
        return self._run(
            lambda cursor: append_publication(
                cursor,
                publication,
                expected_current_revision=expected_current_revision,
                expected_catalog_revision=expected_catalog_revision,
            ),
            preserve=(BindingHeadPreconditionError,),
        )

    def observe_binding_head(self, binding_id: str) -> dict[str, Any]:
        """Read the target, head and catalogue version in one authoritative snapshot."""

        def operation(cursor: _Cursor) -> dict[str, Any]:
            # Bound estimates on fresh databases while retaining duplicate detection.
            cursor.execute(
                "SELECT current_database() AS database_name, current_schema() AS schema_name, "
                "i.installation_id::text AS installation_id, s.revision AS catalog_revision, "
                "h.revision AS current_revision, EXISTS ("
                "SELECT 1 FROM speech_publication_retirements r "
                "WHERE r.kind=h.kind AND r.object_id=h.object_id AND r.revision=h.revision "
                "AND r.revision_id=h.revision_id) AS head_retired FROM speech_binding_families f "
                "CROSS JOIN (SELECT installation_id FROM speech_publication_installation "
                "WHERE singleton IS TRUE LIMIT 2) i "
                "CROSS JOIN (SELECT revision FROM speech_catalog_state "
                "WHERE singleton IS TRUE LIMIT 2) s "
                "LEFT JOIN speech_publication_heads h ON h.kind='binding' "
                "AND h.object_id=f.binding_id "
                "WHERE f.binding_id=%s",
                (binding_id,),
            )
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise PublicationUnavailableError
            if rows[0].pop("head_retired"):
                raise PublicationUnavailableError
            return rows[0]

        return self._run(operation, read_only=True)

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
        *,
        expected_current_revision: object = MISSING_HEAD,
        intent: BindingPublishIntent | None = None,
    ) -> tuple[Publication, BindingPublicationEvidence, bool, str, bool]:
        """Validate the stored triple and capacity evidence, then write both rows."""

        def write_publication(
            cursor: _Cursor,
        ) -> tuple[Publication, BindingPublicationEvidence, bool]:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'speechnexus:catalog:' || current_schema() || ':' || %s || ':' || %s, 0))",
                (publication.kind, publication.object_id),
            )
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

            if append_publication(
                cursor,
                publication,
                expected_current_revision=expected_current_revision,
                expected_catalog_revision=intent.expected_catalog_revision
                if intent is not None
                else MISSING_HEAD,
            ):
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

        def operation(
            cursor: _Cursor,
        ) -> tuple[Publication, BindingPublicationEvidence, bool, str, bool]:
            if intent is None:
                raise BindingHeadPreconditionError("complete_publication_intent_required")
            if expected_current_revision != intent.expected_current_revision:
                raise BindingHeadPreconditionError("observation_mismatch")
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'speechnexus:publish-intent:' || current_schema() || ':' || %s, 0))",
                (intent.key_digest,),
            )
            cursor.execute(
                "SELECT current_database() AS database_name, current_schema() AS schema_name, "
                "installation_id::text AS installation_id FROM speech_publication_installation "
                "WHERE singleton IS TRUE"
            )
            targets = cursor.fetchall()
            if len(targets) != 1 or (
                targets[0]["database_name"],
                targets[0]["schema_name"],
                targets[0]["installation_id"],
            ) != (intent.database, intent.schema, intent.installation_id):
                raise BindingHeadPreconditionError("publication_target_changed")
            payload = {
                "database": intent.database,
                "schema": intent.schema,
                "installation_id": intent.installation_id,
                "expected_current_revision": intent.expected_current_revision,
                "expected_catalog_revision": intent.expected_catalog_revision,
                "binding_id": publication.object_id,
                "revision": str(publication.revision),
                "revision_id": publication.revision_id,
                "capacity_manifest_id": evidence.capacity_manifest_id,
                "qualification_report_id": evidence.qualification_report_id,
                "capacity_manifest_digest": evidence.capacity_manifest_digest,
                "qualification_report_digest": evidence.qualification_report_digest,
                "evidence_source": evidence.evidence_source,
            }
            cursor.execute(
                "SELECT intent, original_created FROM speech_binding_publish_intents "
                "WHERE key_digest=%s",
                (intent.key_digest,),
            )
            rows = cursor.fetchall()
            if len(rows) > 1:
                raise PublicationUnavailableError
            if rows:
                if rows[0]["intent"] != payload:
                    raise BindingIntentConflictError("idempotency_conflict")
                existing = find_publication(
                    cursor, "binding", publication.object_id, publication.revision
                )
                stored_evidence = find_evidence(cursor, publication.object_id, publication.revision)
                if existing is None or stored_evidence is None:
                    raise PublicationUnavailableError
                return existing, stored_evidence, False, "replay", bool(rows[0]["original_created"])
            result = write_publication(cursor)
            cursor.execute(
                "INSERT INTO speech_binding_publish_intents "
                "(key_digest, intent, binding_id, revision, original_created) "
                "VALUES (%s, %s::jsonb, %s, %s, %s)",
                (
                    intent.key_digest,
                    json.dumps(payload),
                    publication.object_id,
                    str(publication.revision),
                    result[2],
                ),
            )
            return (*result, "created" if result[2] else "existing_fact", result[2])

        return self._run(
            operation,
            preserve=(
                BindingRevisionNotFoundError,
                BindingPublicationConflictError,
                BindingCapacityManifestMismatchError,
                BindingEvidenceConflictError,
                BindingHeadPreconditionError,
                BindingIntentConflictError,
            ),
        )

    def get(self, kind: PublicationKind, object_id: str, revision: int) -> Publication | None:
        return self._run(
            lambda cursor: find_publication(cursor, kind, object_id, revision), read_only=True
        )

    def list_kind(self, kind: PublicationKind) -> list[Publication]:
        return self._run(lambda cursor: list_publications(cursor, kind), read_only=True)
