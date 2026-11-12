"""Read-only keyset projection of binding families and their latest revision.

The projection reads only the relations named by the list contract: the family
allocation head, the immutable revision document it names, the publication row
and retirement tombstone for that exact revision, and the mutable publication
head. Commands, idempotency keys, intents, evidence and object-storage references
stay outside this surface, so no private command field can reach the wire.

Rows are ordered by the family primary key ``binding_id`` alone. That column is
unique by construction, so the ordering is total and carries no secondary or
tie-breaking term: a page boundary is a position in the key frontier, not an
offset into a sort over mutable columns such as a revision number, a status or
an insertion time. ``binding_id`` is restricted to ``[a-z0-9._:-]`` and the
database collation is byte-ordered (``C``/``C.UTF-8``, pinned by
``bootstrap.sh`` and the CI cluster), so PostgreSQL and Python compare these
keys identically and the service can reject a page that is not strictly
ascending instead of silently re-sorting it.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.binding_documents import decode_document
from speechnexus_backend.speech.domain.binding_drafts import DOCUMENT_VERSION
from speechnexus_backend.speech.domain.binding_summaries import (
    BindingSummaryQuery,
    BindingSummaryUnavailableError,
    ModelBindingSummary,
    publication_status,
)
from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_COLUMNS = (
    "family.binding_id, family.latest_revision, revision.document_version, "
    "revision.definition_json, revision.definition_digest, "
    "head.revision AS current_revision, "
    "(publication.revision IS NOT NULL) AS published, "
    "retired.mode AS retirement_mode"
)

_BASE = (
    "SELECT " + _COLUMNS + " FROM speech_binding_families AS family "
    "LEFT JOIN speech_binding_revisions AS revision "
    "ON revision.binding_id = family.binding_id "
    "AND revision.revision = family.latest_revision "
    "LEFT JOIN speech_publication_heads AS head "
    "ON head.kind = 'binding' AND head.object_id = family.binding_id "
    "LEFT JOIN speech_publications AS publication "
    "ON publication.kind = 'binding' AND publication.object_id = family.binding_id "
    "AND publication.revision = family.latest_revision "
    "LEFT JOIN speech_publication_retirements AS retired "
    "ON retired.kind = 'binding' AND retired.object_id = family.binding_id "
    "AND retired.revision = family.latest_revision "
    "WHERE family.binding_id > %s"
)

# The predicate must select exactly the rows ``publication_status`` derives;
# a mismatch would make a filtered page omit or duplicate a status.
_STATUS_PREDICATES = {
    "draft": " AND publication.revision IS NULL",
    "published": " AND publication.revision IS NOT NULL AND retired.mode IS NULL",
    "retired": " AND retired.mode = 'retire'",
    "disabled": " AND retired.mode = 'disable'",
}
_ORDER = " ORDER BY family.binding_id LIMIT %s"


def statement(query: BindingSummaryQuery) -> tuple[str, tuple[str, int]]:
    """Compose the single statement this reader may run for one page.

    The returned text is the whole query surface: the selected relations, the
    keyset frontier, the optional status predicate and one total ordering. A
    caller cannot widen it, and the ordering stays a single term so no page can
    depend on the database's incidental row order.
    """
    if not isinstance(query, BindingSummaryQuery):
        raise BindingSummaryUnavailableError
    predicate = "" if query.status is None else _STATUS_PREDICATES[query.status]
    return _BASE + predicate + _ORDER, (query.after_id or "", query.limit + 1)


class PostgresBindingSummaryReads:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 30:
            raise ValueError("Binding summary reads require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def scan(self, query: BindingSummaryQuery) -> tuple[ModelBindingSummary, ...]:
        text, parameters = statement(query)
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BindingSummaryUnavailableError
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                records = connection.execute(text, parameters).fetchall()
                result = tuple(_summary(row) for row in records)
                if time.monotonic() >= deadline:
                    raise BindingSummaryUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            BindingSummaryUnavailableError,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise BindingSummaryUnavailableError from None


def _summary(row: dict[str, Any]) -> ModelBindingSummary:
    latest = row["latest_revision"]
    definition_json = row["definition_json"]
    if (
        not isinstance(latest, str)
        or row["document_version"] != DOCUMENT_VERSION
        or not isinstance(definition_json, str)
    ):
        # A family whose allocation head has no intact revision document cannot
        # be summarised; omitting it would silently shrink the listing.
        raise BindingSummaryUnavailableError
    raw = definition_json.encode("utf-8")
    if document_digest(raw) != row["definition_digest"]:
        raise BindingSummaryUnavailableError
    definition = decode_document(raw)
    logical_model = definition.get("logical_model")
    tasks = definition.get("tasks")
    release_tier = definition.get("release_tier")
    if (
        not isinstance(logical_model, str)
        or not isinstance(tasks, list)
        or not isinstance(release_tier, str)
    ):
        raise BindingSummaryUnavailableError
    return ModelBindingSummary(
        binding_id=row["binding_id"],
        latest_revision=latest,
        current_revision=row["current_revision"],
        logical_model=logical_model,
        tasks=tuple(tasks),
        release_tier=release_tier,
        publication_status=publication_status(
            published=row["published"], retirement_mode=row["retirement_mode"]
        ),
    )
