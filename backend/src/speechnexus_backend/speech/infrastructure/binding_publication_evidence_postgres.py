"""Bulk evidence backfill over one explicit mapping; never invents defaults."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.binding_documents import decode_document
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingCapacityManifestMismatchError,
    BindingEvidenceConflictError,
    BindingPublicationEvidence,
)
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_sql import (
    evidence_snapshot,
    find_binding_revision_document,
    find_evidence,
    insert_evidence,
    list_evidence,
    list_unresolved_publications,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publications_sql import find_publication


class BackfillIncompleteError(RuntimeError):
    """Evidence remains unresolved; the whole backfill transaction must roll back."""


@dataclass(frozen=True, slots=True)
class UnresolvedPublication:
    object_id: str
    revision: int
    revision_id: str


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    publications: int
    evidence: int
    unresolved: tuple[UnresolvedPublication, ...]


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    insert: int
    skip: int
    unresolved_before: int
    unresolved_after: int


@dataclass(frozen=True, slots=True)
class BackfillResult:
    inserted: int
    skipped: int
    evidence_before: int
    evidence_after: int
    unresolved: int


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


class PostgresBindingEvidenceBackfill:
    """Preflight, plan and apply evidence mappings in bounded transactions."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 60) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 300:
            raise ValueError("Evidence backfill requires a bounded deadline")
        self._pool, self._timeout = pool, timeout_seconds

    def _run[T](
        self,
        operation: Callable[[_Cursor], T],
        *,
        read_only: bool,
        preserve: tuple[type[BaseException], ...] = (),
    ) -> T:
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

    def snapshot(self) -> EvidenceSnapshot:
        def operation(cursor: _Cursor) -> EvidenceSnapshot:
            publications, evidence, _ = evidence_snapshot(cursor)
            unresolved = tuple(
                UnresolvedPublication(
                    object_id=row["object_id"],
                    revision=int(row["revision"]),
                    revision_id=row["revision_id"],
                )
                for row in list_unresolved_publications(cursor)
            )
            return EvidenceSnapshot(publications, evidence, unresolved)

        return self._run(operation, read_only=True)

    def export(self) -> tuple[BindingPublicationEvidence, ...]:
        return self._run(lambda cursor: tuple(list_evidence(cursor)), read_only=True)

    def _stage(
        self, cursor: _Cursor, records: Sequence[BindingPublicationEvidence]
    ) -> tuple[list[BindingPublicationEvidence], int]:
        pending: list[BindingPublicationEvidence] = []
        skipped = 0
        for record in records:
            publication = find_publication(cursor, "binding", record.object_id, record.revision)
            if publication is None:
                raise BindingEvidenceConflictError(
                    f"binding_id={record.object_id} revision={record.revision} is not published"
                )
            if publication.revision_id != record.revision_id:
                raise BindingEvidenceConflictError(
                    f"binding_id={record.object_id} revision={record.revision} is published as "
                    f"revision_id={publication.revision_id}, not {record.revision_id}"
                )
            stored = find_binding_revision_document(cursor, record.object_id, record.revision)
            if stored is None or stored[0] != record.revision_id:
                raise BindingEvidenceConflictError(
                    f"binding_id={record.object_id} revision={record.revision} does not store "
                    f"revision_id={record.revision_id}"
                )
            definition = decode_document(stored[1].encode("utf-8"))
            recipe = definition.get("execution_recipe")
            declared = recipe.get("capacity_manifest_id") if isinstance(recipe, dict) else None
            if not isinstance(declared, str) or declared != record.capacity_manifest_id:
                raise BindingCapacityManifestMismatchError(
                    record.object_id,
                    record.revision,
                    declared if isinstance(declared, str) else "missing",
                    record.capacity_manifest_id,
                )
            existing = find_evidence(cursor, record.object_id, record.revision)
            if existing is not None:
                if not existing.same_facts(record):
                    raise BindingEvidenceConflictError(
                        f"binding_id={record.object_id} revision={record.revision} already "
                        "records different publication evidence"
                    )
                skipped += 1
                continue
            pending.append(record)
        return pending, skipped

    def plan(self, records: Sequence[BindingPublicationEvidence]) -> BackfillPlan:
        def operation(cursor: _Cursor) -> BackfillPlan:
            _, _, unresolved = evidence_snapshot(cursor)
            pending, skipped = self._stage(cursor, records)
            return BackfillPlan(
                insert=len(pending),
                skip=skipped,
                unresolved_before=unresolved,
                unresolved_after=max(0, unresolved - len(pending)),
            )

        return self._run(operation, read_only=True)

    def apply(self, records: Sequence[BindingPublicationEvidence]) -> BackfillResult:
        def operation(cursor: _Cursor) -> BackfillResult:
            _, evidence_before, _ = evidence_snapshot(cursor)
            pending, skipped = self._stage(cursor, records)
            inserted = 0
            for record in pending:
                if insert_evidence(cursor, record):
                    inserted += 1
                else:
                    stored = find_evidence(cursor, record.object_id, record.revision)
                    if stored is None or not stored.same_facts(record):
                        raise BindingEvidenceConflictError(
                            f"binding_id={record.object_id} revision={record.revision} "
                            "records conflicting evidence"
                        )
                    skipped += 1
            _, evidence_after, unresolved = evidence_snapshot(cursor)
            if unresolved:
                raise BackfillIncompleteError(
                    f"{unresolved} publication(s) remain without evidence"
                )
            return BackfillResult(inserted, skipped, evidence_before, evidence_after, unresolved)

        return self._run(
            operation,
            read_only=False,
            preserve=(
                BindingCapacityManifestMismatchError,
                BindingEvidenceConflictError,
                BackfillIncompleteError,
            ),
        )
