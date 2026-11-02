"""Published-only facts for synchronous speech, with explicit read deadlines.

The public route may only see immutable publication rows. This reader never
falls back to a draft or an older publication when a selected head is absent;
every missing row becomes ``None`` and its absence is decided by the application.
Exact historical reads remain independent of selection and retirement.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.speech_synthesis import (
    BindingFact,
    WorkspaceFact,
)
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationKind,
    PublicationUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, SpeechPool


class SpeechFactsView:
    """Read projections only; publication is not proof of execution eligibility."""

    def workspace(self, workspace_id: str) -> WorkspaceFact | None:
        rows = self._read(
            "SELECT workspace_state, policy_revision FROM speech_workspaces "
            "WHERE workspace_id = %s",
            (workspace_id,),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicationUnavailableError
        return WorkspaceFact(
            int(rows[0]["policy_revision"]),
            rows[0]["workspace_state"] == "active",
        )

    def latest_binding(self, model_id: str) -> BindingFact | None:
        rows = self._read(
            "SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us, "
            "v.definition_json FROM speech_publications AS p "
            "JOIN speech_publication_heads AS h ON h.kind = p.kind "
            "AND h.object_id = p.object_id AND h.revision = p.revision "
            "AND h.revision_id = p.revision_id "
            "JOIN speech_published_binding_revisions AS v ON v.binding_id = p.object_id "
            "AND v.revision = p.revision AND v.revision_id = p.revision_id "
            "WHERE p.kind = 'binding' AND p.object_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM speech_publication_retirements AS retired "
            "WHERE retired.kind = p.kind AND retired.object_id = p.object_id "
            "AND retired.revision = p.revision AND retired.revision_id = p.revision_id)",
            (model_id,),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicationUnavailableError
        row = rows[0]
        try:
            definition = json.loads(row["definition_json"])
        except json.JSONDecodeError, RecursionError, TypeError, KeyError:
            raise PublicationUnavailableError from None
        if not isinstance(definition, dict):
            raise PublicationUnavailableError
        return BindingFact(_publication(row), definition)

    def latest_publication(self, kind: PublicationKind, object_id: str) -> Publication | None:
        rows = self._read(
            "SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us "
            "FROM speech_publications AS p "
            "JOIN speech_publication_heads AS h ON h.kind = p.kind "
            "AND h.object_id = p.object_id AND h.revision = p.revision "
            "AND h.revision_id = p.revision_id "
            "WHERE p.kind = %s AND p.object_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM speech_publication_retirements AS retired "
            "WHERE retired.kind = p.kind AND retired.object_id = p.object_id "
            "AND retired.revision = p.revision AND retired.revision_id = p.revision_id)",
            (kind, object_id),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicationUnavailableError
        return _publication(rows[0])

    def publication(
        self, kind: PublicationKind, object_id: str, revision: int
    ) -> Publication | None:
        rows = self._read(
            "SELECT kind, object_id, revision, revision_id, published_at_us "
            "FROM speech_publications WHERE kind = %s AND object_id = %s AND revision = %s",
            (kind, object_id, str(revision)),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicationUnavailableError
        return _publication(rows[0])

    def pronunciation_exists(self, object_id: str, revision: int) -> bool:
        rows = self._read(
            "SELECT 1 AS present FROM speech_pronunciation_revisions "
            "WHERE pronunciation_id = %s AND revision = %s",
            (object_id, str(revision)),
        )
        return len(rows) == 1

    def voice_for_request(self, object_id: str, revision: int | None = None) -> Publication | None:
        """Reject retired voices for new requests, preserving exact historical GETs.

        This filter does not certify source, qualification, deletion or revocation.
        It must never be used as the missing production admission authority.
        """
        if revision is None:
            return self.latest_publication("voice", object_id)
        rows = self._read(
            "SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us "
            "FROM speech_publications AS p "
            "WHERE p.kind = 'voice' AND p.object_id = %s AND p.revision = %s "
            "AND NOT EXISTS (SELECT 1 FROM speech_publication_retirements AS retired "
            "WHERE retired.kind = p.kind AND retired.object_id = p.object_id "
            "AND retired.revision = p.revision AND retired.revision_id = p.revision_id)",
            (object_id, str(revision)),
        )
        if len(rows) > 1:
            raise PublicationUnavailableError
        return _publication(rows[0]) if rows else None

    def _read(self, query: str, parameters: tuple[object, ...]) -> list[dict[str, Any]]:
        raise NotImplementedError


class PostgresSpeechFacts(SpeechFactsView):
    """Bounded individual reads or an explicit shared read-only snapshot.

    The snapshot prevents mixed read projections, not retirement after resolution.
    There is no production TTS admission wiring until the lock/intent/trust gaps
    are resolved. No execution or Job binding pin is introduced here.
    """

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Speech facts require a bounded read deadline")
        self._pool = pool
        self._timeout = float(timeout_seconds)

    @contextmanager
    def snapshot(self) -> Iterator[SpeechFactsView]:
        """Read all related facts on one connection; never reuse a view after exit."""
        deadline = time.monotonic() + self._timeout
        view: _SnapshotSpeechFacts | None = None
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                view = _SnapshotSpeechFacts(connection, deadline)
                view.check_deadline()
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                yield view
                view.check_deadline()
            view.check_deadline()
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise PublicationUnavailableError from None
        finally:
            if view is not None:
                view.active = False

    def _read(self, query: str, parameters: tuple[object, ...]) -> list[dict[str, Any]]:
        with self.snapshot() as view:
            return view._read(query, parameters)


class _SnapshotSpeechFacts(SpeechFactsView):
    def __init__(self, connection: SpeechConnection, deadline: float) -> None:
        self._connection = connection
        self._deadline = deadline
        self.active = True

    def check_deadline(self) -> float:
        remaining = self._deadline - time.monotonic()
        if not self.active or remaining <= 0:
            raise PublicationUnavailableError
        return remaining

    def _read(self, query: str, parameters: tuple[object, ...]) -> list[dict[str, Any]]:
        remaining = self.check_deadline()
        self._connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        rows = self._connection.execute(query, parameters).fetchall()
        self.check_deadline()
        return rows


def _publication(row: dict[str, Any]) -> Publication:
    try:
        return Publication(
            kind=row["kind"],
            object_id=row["object_id"],
            revision=int(row["revision"]),
            revision_id=row["revision_id"],
            published_at_us=int(row["published_at_us"]),
        )
    except ValueError, TypeError, KeyError:
        raise PublicationUnavailableError from None


__all__ = ["PostgresSpeechFacts", "SpeechFactsView"]
