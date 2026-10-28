from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from speechnexus_backend.speech.application.binding_disable_coordinator import (
    BindingDisableCoordinator,
)


class Store:
    def __init__(self) -> None:
        self.durable: set[tuple[str, str]] = set()
        self.lose_reply = True

    def event_ids(self, after: str | None, limit: int) -> list[str]:
        return [x for x in ("event-a", "event-b") if after is None or x > after][:limit]

    def capture(self, *args: Any) -> None:
        pass

    def targets(self, event_id: str, after: str | None, limit: int, *_: str) -> list[Any]:
        key = "submit-" + event_id[-1]
        if after is not None and key <= after:
            return []
        return [
            SimpleNamespace(
                submit_intent_id=key, gateway_id="gateway-test", host_node_id="host-test"
            )
        ]

    def begin_abort(self, event_id: str, submit_id: str) -> bool:
        key = event_id, submit_id
        first = key not in self.durable
        self.durable.add(key)
        if event_id == "event-a" and self.lose_reply:
            self.lose_reply = False
            raise psycopg.OperationalError("synthetic-private-connection-detail")
        return first

    def leases(self, *args: Any) -> list[str]:
        return []


class Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def abort_idempotent(self, target: Any) -> object:
        self.calls.append(("abort", target.submit_intent_id))
        return object()

    async def query_disable_target(self, target: Any) -> object:
        self.calls.append(("query", target.submit_intent_id))
        return object()


@pytest.mark.asyncio
async def test_unknown_abort_is_not_sent_root_is_revisited_and_other_root_progresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, gateway = Store(), Gateway()
    coordinator = BindingDisableCoordinator(
        store,
        gateway,
        gateway_id="gateway-test",
        node_id="host-test",
        batch_size=2,
        wait_timeout_ms=1000,
    )
    with caplog.at_level(logging.WARNING):
        first = await coordinator.run_pass()
        assert first.unavailable == 1
        assert gateway.calls == [("abort", "submit-b")]
        await coordinator.run_pass()  # Empty page wraps the root cursor.
        await coordinator.run_pass()
    assert gateway.calls[1:3] == [("query", "submit-a"), ("abort", "submit-a")]
    assert "Disable root unavailable" in caplog.text
    assert "synthetic-private-connection-detail" not in caplog.text
    assert coordinator._target_after == {}  # empty cursors carry no durable meaning
    assert coordinator._lease_after == {}


@pytest.mark.asyncio
async def test_cancellation_propagates_and_does_not_send() -> None:
    class CancelStore(Store):
        def capture(self, *args: Any) -> None:
            raise asyncio.CancelledError

    gateway = Gateway()
    coordinator = BindingDisableCoordinator(
        CancelStore(),
        gateway,
        gateway_id="gateway-test",
        node_id="host-test",
        batch_size=2,
        wait_timeout_ms=1000,
    )
    with pytest.raises(asyncio.CancelledError):
        await coordinator.run_pass()
    assert gateway.calls == []
    assert coordinator._event_after is None
