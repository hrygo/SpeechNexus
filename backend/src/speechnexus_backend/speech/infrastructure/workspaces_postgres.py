"""Bounded, read-only queries of the existing 0003 workspace authority."""

from __future__ import annotations

import time
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.workspaces import (
    Workspace,
    WorkspaceQuery,
    WorkspaceUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_COLUMNS = (
    "workspace_id, display_name, workspace_state, metadata_revision, policy_revision, created_at_us"
)


class PostgresWorkspaceReads:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 30:
            raise ValueError("Workspace reads require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def _read(self, query: str, parameters: tuple[object, ...]) -> tuple[Workspace, ...]:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceUnavailableError
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                records = connection.execute(query, parameters).fetchall()
                result = tuple(_workspace(row) for row in records)
                if time.monotonic() >= deadline:
                    raise WorkspaceUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise WorkspaceUnavailableError from None

    def get(self, workspace_id: str) -> Workspace | None:
        identifier(workspace_id)
        rows = self._read(
            "SELECT " + _COLUMNS + " FROM speech_workspaces WHERE workspace_id = %s",
            (workspace_id,),
        )
        return rows[0] if rows else None

    def scan(self, query: WorkspaceQuery) -> tuple[Workspace, ...]:
        return self._read(
            "SELECT " + _COLUMNS + " FROM speech_workspaces "
            "WHERE workspace_id > %s ORDER BY workspace_id LIMIT %s",
            (query.after_id or "", query.limit + 1),
        )


def _workspace(row: dict[str, Any]) -> Workspace:
    return Workspace(
        row["workspace_id"],
        row["display_name"],
        row["workspace_state"],
        row["metadata_revision"],
        row["policy_revision"],
        row["created_at_us"],
    )
