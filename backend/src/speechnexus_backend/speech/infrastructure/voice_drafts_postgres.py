"""Short PG metadata transactions; lifecycle reads the authoritative generation Job."""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.public_jobs import resolve_voice_previews
from speechnexus_backend.speech.application.voice_drafts import (
    VoiceDraftConflictError,
    VoiceDraftNotFoundError,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobArtifactFact,
    PublicJobUnavailableError,
    canonical_digest,
    idempotency_digest,
    identifier,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, SpeechPool

_SELECT_BASE = (
    "SELECT d.draft_id, d.kind, d.status, d.head_revision, d.metadata_revision, "
    "d.display_name, d.created_at_us, d.updated_at_us, "
    "r.revision, r.generation_job_id, r.input_json, r.binding_json, "
    "r.created_at_us AS revision_created_at_us, "
    "j.state AS job_state, j.result_json, j.error_code, "
    "j.workspace_id AS job_workspace_id, j.updated_at_us AS job_updated_at_us "
    "FROM speech_voice_drafts AS d "
    "JOIN speech_voice_draft_revisions AS r ON r.draft_id = d.draft_id "
    "JOIN speech_jobs AS j ON j.job_id = r.generation_job_id"
)
# The public GET and metadata paths only ever expose the draft head.
_SELECT = _SELECT_BASE + " WHERE d.draft_id=%s AND r.revision = d.head_revision"

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
                or result.get("draft_revision") != row.get("revision", row["head_revision"])
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
            "revision": row.get("revision", row["head_revision"]),
            "kind": row["kind"],
            "status": status,
            "input": json.loads(row["input_json"]),
            "model_binding": json.loads(row["binding_json"]),
            "generation_job_id": row["generation_job_id"],
            "preview_artifact_ids": previews,
            "failure": failure,
            "created_at": _stamp(row.get("revision_created_at_us", row["created_at_us"])),
        },
    }


def _artifact_fact(
    connection: SpeechConnection, workspace_id: str, artifact_id: str
) -> JobArtifactFact | None:
    """Read one ready Artifact inside the caller's transaction."""
    row = connection.execute(
        "SELECT artifact_id, artifact_kind, artifact_state, media_type, bytes, sha256, "
        "receipt_id, expires_at_us FROM speech_artifacts "
        "WHERE workspace_id=%s AND artifact_id=%s",
        (workspace_id, artifact_id),
    ).fetchone()
    if row is None or row["artifact_state"] != "ready" or row["receipt_id"] is None:
        return None
    try:
        return JobArtifactFact(
            artifact_id=row["artifact_id"],
            kind=row["artifact_kind"],
            media_type=row["media_type"],
            bytes=int(row["bytes"]),
            sha256=row["sha256"],
            receipt_id=row["receipt_id"],
            expires_at_us=(None if row["expires_at_us"] is None else int(row["expires_at_us"])),
        )
    except ValueError, TypeError, KeyError:
        raise PublicJobUnavailableError from None


def _require_candidate(connection: SpeechConnection, row: dict[str, Any]) -> None:
    """Reject an unverified candidate before the metadata transaction writes."""
    if _view(row)["revision"]["status"] != "candidate":
        return
    previews = resolve_voice_previews(
        workspace_id=row["job_workspace_id"],
        kind="voice_" + row["kind"],
        state=row["job_state"],
        result_json=row["result_json"],
        artifact=lambda workspace_id, artifact_id: _artifact_fact(
            connection, workspace_id, artifact_id
        ),
    )
    if not previews:
        raise PublicJobUnavailableError


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

    def draft_exists(self, draft_id: str) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT 1 AS present FROM speech_voice_drafts WHERE draft_id = %s",
                (identifier(draft_id),),
            ).fetchone()
        return row is not None

    def get(self, draft_id: str, revision: str | None = None) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = connection.execute(_SELECT, (draft_id,)).fetchone()
        if row is None or (revision is not None and revision != row["head_revision"]):
            # An exact revision selector never silently falls back to the head.
            return None
        return _view(row)

    def revision(self, draft_id: str, revision: str) -> dict[str, Any] | None:
        """Read one immutable revision even when a newer head exists."""
        with self._transaction() as connection:
            row = connection.execute(
                _SELECT_BASE + " WHERE d.draft_id=%s AND r.revision=%s",
                (draft_id, revision),
            ).fetchone()
        if row is None:
            return None
        return cast(dict[str, Any], _view(row)["revision"])

    def revision_target(self, draft_id: str, revision: str) -> dict[str, Any] | None:
        """Read the exact revision a generation or validation request targets."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT d.kind, d.status, d.head_revision, r.binding_json, "
                "j.workspace_id "
                "FROM speech_voice_drafts AS d "
                "JOIN speech_voice_draft_revisions AS r ON r.draft_id = d.draft_id "
                "JOIN speech_jobs AS j ON j.job_id = r.generation_job_id "
                "WHERE d.draft_id=%s AND r.revision=%s",
                (draft_id, revision),
            ).fetchone()
        if row is None:
            return None
        try:
            binding = json.loads(row["binding_json"])
            if not isinstance(binding, dict) or set(binding) != {"id", "revision"}:
                raise ValueError
        except ValueError, TypeError, KeyError:
            raise PublicJobUnavailableError from None
        return {
            "kind": row["kind"],
            "status": row["status"],
            "head_revision": row["head_revision"],
            "workspace_id": row["workspace_id"],
            "binding": binding,
        }

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
                    # Validate the current authority facts before replaying too;
                    # the receipt never contains the full input.
                    _require_candidate(connection, row)
                    recorded: dict[str, Any] = dict(json.loads(original["result_json"]))
                    if "revision" in recorded and isinstance(recorded.get("draft"), dict):
                        return cast(dict[str, Any], recorded["draft"])
                    return recorded
            if (
                row["status"] == "published"
                or row["metadata_revision"] != command["expected_metadata_revision"]
            ):
                raise VoiceDraftConflictError
            # Candidate facts are checked while the row lock and transaction are
            # open. A failure rolls back before the UPDATE or receipt insert.
            _require_candidate(connection, row)
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
            # Only the response projection is replayed; input_json and binding
            # stay in the draft row instead of being copied into the 8192-byte
            # receipt column.
            draft: dict[str, Any] = _view(row)["draft"]
            if key is not None:
                connection.execute(
                    "INSERT INTO speech_voice_metadata_commands "
                    "(draft_id, key_digest, request_digest, result_json) VALUES (%s,%s,%s,%s)",
                    (draft_id, key, digest, json.dumps(draft, ensure_ascii=False)),
                )
        return draft
