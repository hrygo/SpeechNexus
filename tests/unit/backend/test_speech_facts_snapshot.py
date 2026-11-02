"""Synthetic adapter schedules; these do not validate PostgreSQL MVCC or locks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout

from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.speech_facts_postgres import PostgresSpeechFacts


class Connection:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool
        self.snapshot: dict[str, Any] | None = None
        self.rows: list[dict[str, Any]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Connection:
        self.pool.statements.append(query)
        if query == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY":
            self.snapshot = deepcopy(self.pool.current)
        elif query.startswith("SELECT set_config"):
            pass
        else:
            state = self.snapshot if self.snapshot is not None else self.pool.current
            kind = "binding" if "definition_json" in query else "voice"
            revision = state[kind]
            self.rows = [
                {
                    "kind": kind,
                    "object_id": kind,
                    "revision": str(revision),
                    "revision_id": f"{kind}-{revision}",
                    "published_at_us": revision,
                    "definition_json": '{"tasks": ["tts_file"]}',
                }
            ]
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class Pool:
    def __init__(self) -> None:
        self.current = {"binding": 2, "voice": 2}
        self.statements: list[str] = []
        self.calls = 0
        self.active = 0

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Connection]:
        self.calls += 1
        self.active += 1
        try:
            yield Connection(self)
        finally:
            self.active -= 1


def test_shared_snapshot_cannot_mix_binding_before_switch_with_voice_after_switch() -> None:
    pool = Pool()
    facts = PostgresSpeechFacts(cast(SpeechPool, pool))
    with facts.snapshot() as view:
        binding = view.latest_binding("binding")
        assert binding is not None
        # Simulate another transaction committing both selected heads together.
        pool.current = {"binding": 1, "voice": 1}
        voice = view.voice_for_request("voice")
        assert voice is not None
        assert (binding.publication.revision, voice.revision) == (2, 2)
        assert pool.calls == 1 and pool.active == 1
    assert pool.active == 0
    with pytest.raises(PublicationUnavailableError):
        view.latest_binding("binding")
    with facts.snapshot() as next_view:
        assert cast(Any, next_view.latest_binding("binding")).publication.revision == 1
        assert cast(Any, next_view.voice_for_request("voice")).revision == 1


@pytest.mark.parametrize("error", [psycopg.OperationalError(), PoolClosed(), PoolTimeout()])
def test_storage_errors_are_unavailable_without_fallback_or_retry(error: Exception) -> None:
    class BrokenPool(Pool):
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Connection]:
            self.calls += 1
            if self.calls:
                raise error
            yield Connection(self)  # pragma: no cover

    pool = BrokenPool()
    facts = PostgresSpeechFacts(cast(SpeechPool, pool))
    with pytest.raises(PublicationUnavailableError):
        facts.latest_binding("binding")
    assert pool.calls == 1 and not pool.statements


def test_expired_connection_queue_budget_executes_no_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]

    class DelayedPool(Pool):
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Connection]:
            clock[0] = 4.0
            yield Connection(self)

    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.speech_facts_postgres.time.monotonic",
        lambda: clock[0],
    )
    pool = DelayedPool()
    facts = PostgresSpeechFacts(cast(SpeechPool, pool), timeout_seconds=3)
    with pytest.raises(PublicationUnavailableError):
        facts.latest_binding("binding")
    assert pool.statements == []


def test_snapshot_is_invalidated_on_consumer_error() -> None:
    pool = Pool()
    with (
        pytest.raises(RuntimeError),
        PostgresSpeechFacts(cast(SpeechPool, pool)).snapshot() as view,
    ):
        raise RuntimeError("synthetic consumer abort")
    with pytest.raises(PublicationUnavailableError):
        view.voice_for_request("voice")
    assert pool.active == 0


def test_sql_failure_discards_view_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    execute = Connection.execute

    def fail_query(
        self: Connection,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> Connection:
        if "FROM speech_publications" in query:
            raise psycopg.errors.InsufficientPrivilege("synthetic denied")
        return execute(self, query, parameters)

    monkeypatch.setattr(Connection, "execute", fail_query)
    pool = Pool()
    facts = PostgresSpeechFacts(cast(SpeechPool, pool))
    with pytest.raises(PublicationUnavailableError), facts.snapshot() as view:
        view.latest_binding("binding")
    assert pool.calls == 1 and pool.active == 0
    with pytest.raises(PublicationUnavailableError):
        view.latest_binding("binding")
