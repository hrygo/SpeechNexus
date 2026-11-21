"""Bounded PG authority; any unreadable original result remains unknown."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.profile_documents import check_command
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.profile_drafts import (
    CreateProfileDraft,
    ProfileDraft,
    ProfileDraftResult,
    ProfileUnavailableError,
    revision_number,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.profile_drafts_sql import (
    create_draft,
    read_draft,
    recover_draft,
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
            raise ProfileUnavailableError
        self.raw.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self.raw.execute(query, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.raw.fetchall()

    def clock(self) -> int:
        self.execute("SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp())*1000000 AS BIGINT) AS t")
        return cast(int, self.fetchall()[0]["t"])


class PostgresProfileDrafts:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Profile transactions require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def _run[T](self, operation: Callable[[_Cursor], T], *, read_only: bool = False) -> T:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                if read_only:
                    connection.execute("SET TRANSACTION READ ONLY")
                with connection.cursor() as raw:
                    result = operation(_Cursor(raw, deadline))
                    if time.monotonic() >= deadline:
                        raise ProfileUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ProfileUnavailableError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            raise ProfileUnavailableError from None

    def create(self, command: CreateProfileDraft) -> ProfileDraftResult:
        check_command(command)
        try:
            return self._run(lambda cursor: create_draft(cursor, command, cursor.clock))
        except ProfileUnavailableError:
            return ProfileDraftResult("unknown")

    def recover(self, command: CreateProfileDraft) -> ProfileDraftResult:
        check_command(command)
        try:
            return self._run(lambda cursor: recover_draft(cursor, command), read_only=True)
        except ProfileUnavailableError:
            return ProfileDraftResult("unknown")

    def get(self, profile_id: str, revision: str) -> ProfileDraft | None:
        identifier(profile_id)
        revision_number(revision)
        return self._run(lambda cursor: read_draft(cursor, profile_id, revision), read_only=True)
