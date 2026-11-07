"""Short PG metadata transactions; lifecycle reads the authoritative generation Job."""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.voice_drafts import (
    VoiceDraftConflictError,
    VoiceDraftNotFoundError,
)
from speechnexus_backend.speech.domain.public_jobs import (
    PublicJobUnavailableError,
    canonical_digest,
    idempotency_digest,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, SpeechPool

_SELECT = (
    "SELECT d.*, j.state AS job_state, j.result_json, j.error_code, "
    "j.updated_at_us AS job_updated_at_us "
    "FROM speech_voice_drafts d JOIN speech_jobs j ON j.job_id=d.generation_job_id "
    "WHERE d.draft_id=%s"
)

# A terminal Job without a stored error code (cancelled or expired) still owes the
# public revision a named failure: the contract forbids status=failed without one.
_TERMINAL_FAILURE_CODES = {
    "cancelled": "voice_generation_cancelled",
    "expired": "voice_generation_expired",
}
_TERMINAL_FAILURE_MESSAGES = {
    "failed": "The generation job failed.",
    "cancelled": "The generation job was cancelled.",
    "expired": "The generation job expired.",
}


def _failure(state: str, error_code: str | None) -> dict[str, Any]:
    code = error_code if state == "failed" and error_code else _TERMINAL_FAILURE_CODES.get(state)
    return {
        "code": code or "voice_generation_failed",
        "message": _TERMINAL_FAILURE_MESSAGES.get(state, "The generation job did not complete."),
        "retryable": False,
    }


def _stamp(value: int) -> str:
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)).isoformat()


def _view(row: dict[str, Any]) -> dict[str, Any]:
    status = row["status"]
    previews: list[str] = []
    failure: dict[str, Any] | None = None
    if status == "generating":
        if row["job_state"] == "succeeded":
            result = json.loads(row["result_json"])
            if (
                result.get("voice_draft_id") != row["draft_id"]
                or result.get("draft_revision") != row["head_revision"]
                or result.get("kind") != "voice_" + row["kind"]
            ):
                raise PublicJobUnavailableError
            status, previews = "candidate", result["preview_artifact_ids"]
        elif row["job_state"] in ("failed", "cancelled", "expired"):
            status = "failed"
            failure = _failure(row["job_state"], row.get("error_code"))
    draft = {
        "resource_type": "voice_draft",
        "object": "nexus.voice_draft",
        "id": row["draft_id"],
        "kind": row["kind"],
        "status": status,
        "head_revision": row["head_revision"],
        "metadata_revision": row["metadata_revision"],
        "display_name": row["display_name"],
        "created_at": _stamp(row["created_at_us"]),
        "updated_at": _stamp(max(row["updated_at_us"], row["job_updated_at_us"])),
    }
    return {
        "object": "nexus.voice_draft_view",
        "draft": draft,
        "revision": {
            "resource_type": "voice_draft_revision",
            "object": "nexus.voice_draft_revision",
            "draft_id": row["draft_id"],
            "revision": row["head_revision"],
            "kind": row["kind"],
            "status": status,
            "input": json.loads(row["input_json"]),
            "model_binding": json.loads(row["binding_json"]),
            "generation_job_id": row["generation_job_id"],
            "preview_artifact_ids": previews,
            "failure": failure,
            "created_at": _stamp(row["created_at_us"]),
        },
    }


class PostgresVoiceDrafts:
    def __init__(self, pool: SpeechPool) -> None:
        self._pool = pool

    @contextmanager
    def _transaction(self) -> Iterator[SpeechConnection]:
        try:
            with self._pool.connection(timeout=3) as connection, connection.transaction():
                connection.execute("SET LOCAL statement_timeout='3s'")
                yield connection
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            # Includes a lost COMMIT reply. Only the original key may be replayed.
            raise PublicJobUnavailableError from None

    def get(self, draft_id: str, revision: str | None = None) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = connection.execute(_SELECT, (draft_id,)).fetchone()
        if row is None or (revision is not None and revision != row["head_revision"]):
            # An exact revision selector never silently falls back to the head.
            return None
        return _view(row)

    def update_metadata(
        self, draft_id: str, command: Mapping[str, Any], idempotency_key: str | None
    ) -> dict[str, Any]:
        digest = canonical_digest(dict(command))
        key = None if idempotency_key is None else idempotency_digest(idempotency_key)
        with self._transaction() as connection:
            # The draft lock serializes metadata CAS and receipt lookup. Replay
            # precedes CAS/publication checks, so a lost reply remains recoverable.
            row = connection.execute(_SELECT + " FOR UPDATE OF d", (draft_id,)).fetchone()
            if row is None:
                raise VoiceDraftNotFoundError
            if key is not None:
                original = connection.execute(
                    "SELECT request_digest, result_json FROM speech_voice_metadata_commands "
                    "WHERE draft_id=%s AND key_digest=%s",
                    (draft_id, key),
                ).fetchone()
                if original is not None:
                    if original["request_digest"] != digest:
                        raise VoiceDraftConflictError
                    return dict(json.loads(original["result_json"]))
            if (
                row["status"] == "published"
                or row["metadata_revision"] != command["expected_metadata_revision"]
            ):
                raise VoiceDraftConflictError
            changed = connection.execute(
                "UPDATE speech_voice_drafts SET display_name=%s, "
                "metadata_revision=metadata_revision+1, "
                "updated_at_us=GREATEST(updated_at_us, "
                "(EXTRACT(EPOCH FROM clock_timestamp())*1000000)::bigint) "
                "WHERE draft_id=%s RETURNING *",
                (command["display_name"], draft_id),
            ).fetchone()
            if changed is None:
                raise PublicJobUnavailableError
            row = {**row, **changed}
            view = _view(row)
            if key is not None:
                connection.execute(
                    "INSERT INTO speech_voice_metadata_commands "
                    "(draft_id, key_digest, request_digest, result_json) VALUES (%s,%s,%s,%s)",
                    (draft_id, key, digest, json.dumps(view, ensure_ascii=False)),
                )
        return view
