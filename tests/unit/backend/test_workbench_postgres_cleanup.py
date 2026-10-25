"""A stalled transaction exit cannot retain a connection slot after its deadline."""

from __future__ import annotations

import asyncio
from types import TracebackType

import psycopg
import pytest

from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    VersionStoreError,
)


class _Socket:
    def __init__(self) -> None:
        self.closed = False

    def finish(self) -> None:
        self.closed = True


class _Transaction:
    def __init__(self, stall: bool) -> None:
        self.release = asyncio.Event()
        self.entered_exit = asyncio.Event()
        self.finished_exit = asyncio.Event()
        if not stall:
            self.release.set()

    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.entered_exit.set()
        try:
            await self.release.wait()
        finally:
            self.finished_exit.set()


class _Connection:
    def __init__(self, *, stall: bool) -> None:
        self.pgconn = _Socket()
        self.tx = _Transaction(stall)

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.pgconn.finish()

    async def execute(self, query: str, params: object = None) -> None:
        pass

    def transaction(self) -> _Transaction:
        return self.tx


@pytest.mark.parametrize("phase", ["body_deadline", "commit", "body_error", "external_cancel"])
async def test_stalled_cleanup_closes_socket_releases_slot_and_invalidates_session(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    connections: list[_Connection] = []

    async def connect(*args: object, **kwargs: object) -> _Connection:
        connection = _Connection(stall=not connections)
        connections.append(connection)
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    uow = PostgresVersionUnitOfWork(
        "synthetic",
        schema="workbench_test",
        max_connections=1,
        transaction_timeout=0.02,
        cleanup_timeout=0.03,
    )
    entered = asyncio.Event()
    sessions = []

    async def mutation() -> None:
        async with uow.transaction() as session:
            sessions.append(session)
            entered.set()
            if phase == "body_error":
                raise ValueError("synthetic body failure")
            if phase != "commit":
                await asyncio.Event().wait()

    task = asyncio.create_task(mutation())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if phase == "external_cancel":
            task.cancel()
        await asyncio.sleep(0.15)
        assert task.done(), "transaction exit retained its connection beyond the deadline"
        with pytest.raises((VersionStoreError, ValueError, asyncio.CancelledError)) as error:
            await task
        if phase == "commit":
            assert isinstance(error.value, VersionStoreError) and error.value.commit_unknown
        elif phase == "external_cancel":
            assert isinstance(error.value, asyncio.CancelledError)
        elif phase == "body_error":
            assert isinstance(error.value, ValueError)
        assert connections[0].pgconn.closed
        await asyncio.wait_for(connections[0].tx.finished_exit.wait(), 1)
        with pytest.raises(RuntimeError, match="closed"):
            await sessions[0].now()
        # Reacquire the same singleton UoW's only slot after the failed request.
        async with uow.transaction():
            pass
        assert len(connections) == 2 and connections[1].pgconn.closed
    finally:
        for connection in connections:
            connection.tx.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
