"""Bounded read-only lookup of committed Artifact metadata."""

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
        )
    except KeyError, TypeError, ValueError:
        raise ArtifactError("artifact_storage_unavailable") from None


__all__ = ["PostgresArtifactMetadata"]
