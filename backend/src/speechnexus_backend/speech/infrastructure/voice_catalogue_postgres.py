"""Bounded reads of immutable published voice facts, never approval originals."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from speechnexus_contracts.voice_catalogue_http import validate_response

from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import PublicationCursor
from speechnexus_backend.speech.infrastructure.voice_catalogue_schema import VOICE_READER_VIEW


def _document(row: dict[str, Any]) -> dict[str, Any]:
    if row["document"] is None:
        raise PublicationUnavailableError
    body: dict[str, Any] = deepcopy(row["document"])
    validate_response("getVoiceRevision", 200, {"body": body})
    if (body["voice_id"], body["revision"], body["status"]) != (
        row["voice_id"],
        row["revision"],
        "published",
    ):
        raise ValueError("Published voice identity mismatch")
    published = datetime.fromisoformat(body["published_at"].replace("Z", "+00:00"))
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    if published != epoch + timedelta(microseconds=row["published_at_us"]):
        raise ValueError("Published voice time mismatch")
    if row["retirement_mode"] is not None:
        body["status"] = "disabled" if row["retirement_mode"] == "disable" else "retired"
        body["retired_at"] = (
            (epoch + timedelta(microseconds=row["retired_at_us"]))
            .isoformat()
            .replace("+00:00", "Z")
        )
        body["disabled_reason"] = row["retirement_reason"] if body["status"] == "disabled" else None
    validate_response("getVoiceRevision", 200, {"body": body})
    return body


def _require_publications(cursor: PublicationCursor) -> None:
    cursor.execute(f"SELECT 1 FROM {VOICE_READER_VIEW} LIMIT 1")
    if not cursor.fetchall():
        raise PublicationUnavailableError


class PostgresVoiceCatalogue:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        self._driver = PostgresPublications(pool, timeout_seconds=timeout_seconds)

    def list_voices(
        self, *, language: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        def read(db: PublicationCursor) -> dict[str, Any]:
            _require_publications(db)
            # Check publication integrity before filters or pagination can hide
            # a selected fact whose exact qualification is unavailable.
            db.execute(
                f"SELECT 1 FROM {VOICE_READER_VIEW} WHERE selected "
                "AND retired_at_us IS NULL AND (document IS NULL "
                "OR document->>'voice_id' IS DISTINCT FROM voice_id "
                "OR document->>'revision' IS DISTINCT FROM revision) LIMIT 1"
            )
            if db.fetchall():
                raise PublicationUnavailableError
            db.execute(
                f"SELECT * FROM {VOICE_READER_VIEW} WHERE selected AND retired_at_us IS NULL "
                "AND (%s::text IS NULL OR document->'capabilities'->'languages' ? %s) "
                "AND (%s::text IS NULL OR voice_id > %s) ORDER BY voice_id LIMIT %s",
                (language, language, cursor, cursor, limit + 1),
            )
            rows = db.fetchall()
            documents = [_document(row) for row in rows]
            data = [
                {
                    "id": doc["voice_id"],
                    "revision": doc["revision"],
                    "display_name": doc["display_name"],
                    "source_type": doc["source"]["type"],
                    "status": doc["status"],
                    "languages": doc["capabilities"]["languages"],
                    "audio_formats": doc["capabilities"]["audio_formats"],
                }
                for doc in documents[:limit]
            ]
            more = len(rows) > limit
            return {
                "object": "nexus.list",
                "data": data,
                "has_more": more,
                "next_cursor": data[-1]["id"] if more else None,
            }

        return self._driver._run(read, read_only=True, repeatable_read=True)

    def get_revision(self, voice_id: str, revision: str) -> dict[str, Any] | None:
        def read(db: PublicationCursor) -> dict[str, Any] | None:
            _require_publications(db)
            db.execute(
                f"SELECT * FROM {VOICE_READER_VIEW} WHERE voice_id=%s AND revision=%s",
                (voice_id, revision),
            )
            rows = db.fetchall()
            return _document(rows[0]) if rows else None

        return self._driver._run(read, read_only=True, repeatable_read=True)
