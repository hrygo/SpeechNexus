"""Bounded coordinator rotation and transient worker failures."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest

from speechnexus_backend.binding_disable_worker import _run_loop
from speechnexus_backend.speech.application.binding_disable_coordinator import (
    BindingDisableCoordinator,
    DisablePassResult,
)
from speechnexus_backend.speech.domain.leases import LeaseResult, unknown


@pytest.mark.asyncio
async def test_worker_resumes_after_redacted_transient_database_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Coordinator:
        calls = 0

        async def run_pass(self) -> DisablePassResult:
            self.calls += 1
            if self.calls == 1:
                raise psycopg.OperationalError("synthetic-private-provider-detail")
            raise asyncio.CancelledError

    coordinator = Coordinator()
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await _run_loop(cast(Any, coordinator), 0)
    assert coordinator.calls == 2
    assert "Disable scan unavailable" in caplog.text
    assert "synthetic-private-provider-detail" not in caplog.text


@pytest.mark.asyncio
async def test_bounded_rotation_advances_past_root_with_many_pending_pages() -> None:
    class Store:
        def event_ids(self, after: str | None, limit: int) -> list[str]:
            return [item for item in ("event-a", "event-b") if after is None or item > after][
                :limit
            ]

        def capture(self, *args: Any) -> None:
            pass

        def targets(self, event: str, after: str | None, limit: int, *_: str) -> list[Any]:
            names = [f"submit-a-{i}" for i in range(8)] if event == "event-a" else ["submit-b-0"]
            return [
                SimpleNamespace(
                    submit_intent_id=x, gateway_id="gateway-test", host_node_id="host-test"
                )
                for x in names
                if after is None or x > after
            ][:limit]

        def begin_abort(self, event: str, submit: str) -> bool:
            return True

        def leases(
            self, event: str, gateway: str, node: str, after: str | None, limit: int
        ) -> list[str]:
            names = [f"lease-a-{i}" for i in range(8)] if event == "event-a" else ["lease-b-0"]
            return [x for x in names if after is None or x > after][:limit]

    class Gateway:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def abort_idempotent(self, target: Any) -> object:
            self.sent.append(target.submit_intent_id)
            return object()

        async def query_disable_target(self, target: Any) -> object:
            return object()

    class Settlement:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def query(self, lease_id: str) -> LeaseResult:
            self.seen.append(lease_id)
            return unknown(lease_id)

        def release(self, request: Any) -> LeaseResult:
            raise AssertionError("unknown lease must never be released")

    gateway, settlement = Gateway(), Settlement()
    coordinator = BindingDisableCoordinator(
        Store(),
        gateway,
        gateway_id="gateway-test",
        node_id="host-test",
        batch_size=1,
        wait_timeout_ms=1000,
        settlement=settlement,
    )
    first = await coordinator.run_pass()
    second = await coordinator.run_pass()
    assert first.attempted == second.attempted == 1
    assert gateway.sent == ["submit-a-0", "submit-b-0"]
    assert settlement.seen == ["lease-a-0", "lease-b-0"]
    assert first.pending == second.pending == 1
    await coordinator.run_pass()  # End of root page; this only wraps the cursor.
    await coordinator.run_pass()
    assert gateway.sent[-1] == "submit-a-1"
    assert settlement.seen[-1] == "lease-a-1"
    restarted = BindingDisableCoordinator(
        Store(),
        gateway,
        gateway_id="gateway-test",
        node_id="host-test",
        batch_size=1,
        wait_timeout_ms=1000,
        settlement=settlement,
    )
    await restarted.run_pass()
    assert gateway.sent[-1] == "submit-a-0"  # Restart revisits durable original identities.
