"""Bounded lookup and committed insertion of Artifact metadata."""

from __future__ import annotations

import time
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.artifacts import ArtifactError, ArtifactRecord
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class PostgresArtifactMetadata:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Artifact reads require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def get(self, artifact_id: str, workspace_id: str) -> tuple[ArtifactRecord | None, int]:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ArtifactError("artifact_storage_unavailable")
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                rows = connection.execute(
                    "SELECT a.artifact_id, a.workspace_id, a.artifact_kind, a.artifact_state, "
                    "a.media_type, a.bytes, a.sha256, a.receipt_id, a.source_operation_id, "
                    "a.created_at_us, a.expires_at_us, a.object_key, a.storage_version_id, "
                    "a.deleted_at_us, "
                    "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us "
                    "FROM speech_artifacts AS a JOIN speech_workspaces AS w "
                    "ON w.workspace_id = a.workspace_id "
                    "WHERE a.artifact_id = %s AND a.workspace_id = %s "
                    "AND w.workspace_state = 'active'",
                    (identifier(artifact_id), identifier(workspace_id)),
                ).fetchall()
        except ArtifactError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise ArtifactError("artifact_storage_unavailable") from None
        if not rows:
            return None, self._now_us()
        if len(rows) != 1:
            raise ArtifactError("artifact_storage_unavailable")
        return _artifact(rows[0]), cast(int, rows[0]["now_us"])

    @staticmethod
    def _now_us() -> int:
        return time.time_ns() // 1_000


# Writable columns, in insert order: exactly `ARTIFACT_INSERT_COLUMNS`, so the
# publisher's grant and this statement cannot drift apart.
_COLUMNS = (
    "artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, "
    "sha256, receipt_id, source_operation_id, created_at_us, expires_at_us, "
    "object_key, storage_version_id"
)
# Projection of a committed row, which also carries the durable removal record.
_READ_COLUMNS = _COLUMNS + ", deleted_at_us"


class PostgresArtifactWriter:
    """Insert one ready Artifact row without ever calling object storage here."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Artifact writes require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def find(self, artifact_id: str) -> ArtifactRecord | None:
        artifact_id = identifier(artifact_id)
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                row = connection.execute(
                    f"SELECT {_READ_COLUMNS} FROM speech_artifacts WHERE artifact_id = %s",
                    (artifact_id,),
                ).fetchone()
        except ArtifactError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise ArtifactError("artifact_storage_unavailable") from None
        return None if row is None else _artifact(row)

    def insert_ready(self, artifact: ArtifactRecord) -> ArtifactRecord:
        """Commit one ready row; an existing deterministic row is returned unchanged."""
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                inserted = connection.execute(
                    "INSERT INTO speech_artifacts ("
                    + _COLUMNS
                    + ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (artifact_id) DO NOTHING RETURNING " + _READ_COLUMNS,
                    (
                        artifact.artifact_id,
                        artifact.workspace_id,
                        artifact.kind,
                        artifact.status,
                        artifact.media_type,
                        artifact.bytes,
                        artifact.sha256,
                        artifact.receipt_id,
                        artifact.source_operation_id,
                        artifact.created_at_us,
                        artifact.expires_at_us,
                        artifact.object_key,
                        artifact.storage_version_id,
                    ),
                ).fetchone()
                row = (
                    inserted
                    or connection.execute(
                        f"SELECT {_READ_COLUMNS} FROM speech_artifacts WHERE artifact_id = %s",
                        (artifact.artifact_id,),
                    ).fetchone()
                )
        except ArtifactError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise ArtifactError("artifact_storage_unavailable") from None
        if row is None:
            raise ArtifactError("artifact_storage_unavailable")
        return _artifact(row)


def _artifact(row: dict[str, Any]) -> ArtifactRecord:
    try:
        return ArtifactRecord(
            row["artifact_id"],
            row["workspace_id"],
            row["artifact_kind"],
            row["artifact_state"],
            row["media_type"],
            int(row["bytes"]),
            row["sha256"],
            row["receipt_id"],
            row["source_operation_id"],
            int(row["created_at_us"]),
            None if row["expires_at_us"] is None else int(row["expires_at_us"]),
            row["object_key"],
            row["storage_version_id"],
            None if row["deleted_at_us"] is None else int(row["deleted_at_us"]),
        )
    except KeyError, TypeError, ValueError:
        raise ArtifactError("artifact_storage_unavailable") from None


__all__ = ["PostgresArtifactMetadata", "PostgresArtifactRetention", "PostgresArtifactWriter"]


# One database-clock expression, so the retention decision, the selection and the
# state transition all read the same instant instead of a process clock.
_NOW_US = "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT)"
_ELAPSED = "expires_at_us IS NOT NULL AND expires_at_us <= " + _NOW_US


class PostgresArtifactRetention:
    """Bounded expiry transitions guarded by the recorded retention deadline.

    Every statement restates the retention predicate: the database clock must be
    at or past ``expires_at_us``, the deadline must exist, and the state must be
    exactly the one the transition expects. A caller therefore cannot expire or
    remove content inside its window, and the selected deadline plus the state
    change are the evidence a later review can replay.
    """

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Artifact retention requires bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def expired(self, limit: int) -> tuple[int, tuple[ArtifactRecord, ...]]:
        """Committed `ready` rows whose retention elapsed, oldest deadline first."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Artifact retention must be bounded")
        return self._read(
            "SELECT " + _READ_COLUMNS + ", " + _NOW_US + " AS now_us FROM speech_artifacts "
            "WHERE artifact_state = 'ready' AND " + _ELAPSED + " "
            "ORDER BY expires_at_us, artifact_id LIMIT %s",
            (limit,),
        )

    def mark_expired(self, artifact_id: str) -> ArtifactRecord | None:
        """Move one elapsed row to `expired`, or report that another pass did."""
        row = self._transition(
            "UPDATE speech_artifacts SET artifact_state = 'expired' "
            "WHERE artifact_id = %s AND artifact_state = 'ready' AND " + _ELAPSED + " "
            "RETURNING " + _READ_COLUMNS,
            artifact_id,
        )
        return None if row is None else _artifact(row)

    def pending_removal(self, limit: int) -> tuple[int, tuple[ArtifactRecord, ...]]:
        """`expired` rows whose retention elapsed, oldest deadline first."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Artifact retention must be bounded")
        return self._read(
            "SELECT " + _READ_COLUMNS + ", " + _NOW_US + " AS now_us FROM speech_artifacts "
            "WHERE artifact_state = 'expired' AND " + _ELAPSED + " "
            "ORDER BY expires_at_us, artifact_id LIMIT %s",
            (limit,),
        )

    def mark_deleted(self, artifact_id: str) -> ArtifactRecord | None:
        """Record a confirmed content removal and when it happened.

        The removal time comes from the same database clock as the deadline check
        and is written by the same statement as the state change, so the ledger
        cannot disagree with the state. `None` means another pass (or another
        path) moved the row first.
        """
        row = self._transition(
            "UPDATE speech_artifacts SET artifact_state = 'deleted', deleted_at_us = "
            + _NOW_US
            + " WHERE artifact_id = %s AND artifact_state = 'expired' AND "
            + _ELAPSED
            + " RETURNING "
            + _READ_COLUMNS,
            artifact_id,
        )
        return None if row is None else _artifact(row)

    def current(self, artifact_id: str) -> ArtifactRecord | None:
        """Read the row as it stands now, for explaining a refused transition."""
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                row = connection.execute(
                    "SELECT " + _READ_COLUMNS + " FROM speech_artifacts WHERE artifact_id = %s",
                    (identifier(artifact_id),),
                ).fetchone()
        except ArtifactError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise ArtifactError("artifact_storage_unavailable") from None
        return None if row is None else _artifact(row)

    def _read(
        self, statement: str, parameters: tuple[object, ...]
    ) -> tuple[int, tuple[ArtifactRecord, ...]]:
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                rows = connection.execute(statement, parameters).fetchall()
        except ArtifactError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise ArtifactError("artifact_storage_unavailable") from None
        if not rows:
            return self._now_us(), ()
        now_us = rows[0]["now_us"]
        if type(now_us) is not int:
            raise ArtifactError("artifact_storage_unavailable")
        return now_us, tuple(_artifact(row) for row in rows)

    def _transition(self, statement: str, artifact_id: str) -> dict[str, Any] | None:
        """Run one guarded state transition; `None` means another pass won."""
        artifact_id = identifier(artifact_id)
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                return connection.execute(statement, (artifact_id,)).fetchone()
        except ArtifactError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise ArtifactError("artifact_storage_unavailable") from None

    @staticmethod
    def _now_us() -> int:
        return time.time_ns() // 1_000
