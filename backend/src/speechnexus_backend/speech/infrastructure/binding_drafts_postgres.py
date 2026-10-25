"""Bounded PostgreSQL draft registry; unknown results never authorize a new identity."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.binding_documents import check_command
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.binding_drafts import (
    BindingDraft,
    BindingDraftResult,
    BindingUnavailableError,
    CreateBindingDraft,
)
from speechnexus_backend.speech.domain.configuration import revision_number
from speechnexus_backend.speech.infrastructure.binding_drafts_sql import (
    create_draft,
    read_draft,
    recover_draft,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class _Cursor:
    def __init__(self, cursor: psycopg.Cursor[dict[str, Any]], deadline: float) -> None:
        self.raw, self.deadline = cursor, deadline

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BindingUnavailableError
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


class PostgresBindingDrafts:
    def __init__(
        self,
        pool: SpeechPool,
        *,
        timeout_seconds: float = 3,
        identity_factory: Callable[[], str] | None = None,
    ) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Binding transactions require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds
        self._new_identity = identity_factory or (lambda: "mbrev-" + uuid.uuid4().hex)

    def _run[T](self, operation: Callable[[_Cursor], T], *, read_only: bool = False) -> T:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                if read_only:
                    connection.execute("SET TRANSACTION READ ONLY")
                with connection.cursor() as raw:
                    result = operation(_Cursor(raw, deadline))
                    if time.monotonic() >= deadline:
                        raise BindingUnavailableError
            if time.monotonic() >= deadline:
                raise BindingUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            BindingUnavailableError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            raise BindingUnavailableError from None

    def create(self, command: CreateBindingDraft) -> BindingDraftResult:
        check_command(command)
        if command.parameter_defaults is not None:
            raise ValueError("Default declarations require the atomic snapshot port")
        try:
            return self._run(
                lambda cursor: create_draft(cursor, command, cursor.clock, self._new_identity)
            )
        except BindingUnavailableError:
            return BindingDraftResult("unknown")

    def recover(self, command: CreateBindingDraft) -> BindingDraftResult:
        check_command(command)
        if command.parameter_defaults is not None:
            raise ValueError("Default declarations require the atomic snapshot port")
        try:
            return self._run(lambda cursor: recover_draft(cursor, command), read_only=True)
        except BindingUnavailableError:
            return BindingDraftResult("unknown")

    def get(self, binding_id: str, revision: str) -> BindingDraft | None:
        identifier(binding_id)
        revision_number(revision)
        return self._run(lambda cursor: read_draft(cursor, binding_id, revision), read_only=True)
