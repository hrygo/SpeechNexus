"""ASR settlement only asks for release; it never invents an authorization."""

from __future__ import annotations

from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.asr_settlement import AsrSettlementWorker
from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.asr_settlement import AsrSettlementRoot
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.leases import (
    LeaseRecord,
    LeaseResult,
    rejected,
    unknown,
)
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    SettlementPreflightUnavailableError,
)

ROOT = AsrSettlementRoot(
    "lease-test", "operation-test", "attempt-test", "gateway-test", "host-test"
)


def lease(state: str = "reserved") -> LeaseRecord:
    return LeaseRecord(
        "lease-test",
        state,
        "complete" if state == "released" else "pending",
        1,
        PhysicalResources(0, 0, 0, 0, 0, 0),
        1,
        budget_released_at_us=1 if state == "released" else None,
    )


class Store:
    def __init__(self, *, intent: str | None = "submit-test") -> None:
        self._intent = intent
        self.marks = 0
        self.deferrals = 0

    def due(self, gateway_id: str, host_node_id: str, limit: int) -> list[AsrSettlementRoot]:
        return [ROOT]

    def intent(self, lease_id: str) -> str | None:
        return self._intent

    def defer(self, lease_id: str, *, seconds: float) -> None:
        self.deferrals += 1

    def mark_settled(self, lease_id: str) -> None:
        self.marks += 1


class Leases:
    def __init__(self, *, released: bool = False, commit_unknown: bool = False) -> None:
        self.current = lease("released" if released else "reserved")
        self.calls = 0
        self.commit_unknown = commit_unknown

    def query(self, lease_id: str) -> LeaseResult:
        return LeaseResult("replayed", lease_id, self.current)

    def release(self, request: Any) -> LeaseResult:
        self.calls += 1
        if self.commit_unknown:
            self.current = lease("released")
            return unknown(request.lease_id)
        self.current = lease("released")
        return LeaseResult("committed", request.lease_id, self.current)


class Gateway:
    def __init__(self, state: str | None) -> None:
        self.state = state
        self.queries = 0

    async def query(self, submit_intent_id: str) -> Any:
        self.queries += 1
        if self.state is None:
            raise OSError("synthetic unavailable")
        return {"execution_state": self.state}


def worker(store: Store, gateway: Gateway, leases: Leases) -> AsrSettlementWorker:
    return AsrSettlementWorker(
        cast(LeaseService, leases),
        store,
        gateway,
        gateway_id="gateway-test",
        host_node_id="host-test",
    )


@pytest.mark.asyncio
async def test_terminal_execution_releases_once_and_marks_settled() -> None:
    store, gateway, leases = Store(), Gateway("succeeded"), Leases()
    result = await worker(store, gateway, leases).run_once()
    assert (result.scanned, result.released, result.pending) == (1, 1, 0)
    assert (leases.calls, store.marks, store.deferrals) == (1, 1, 0)


@pytest.mark.asyncio
async def test_unknown_quarantined_is_not_terminal_and_never_releases() -> None:
    store, gateway, leases = Store(), Gateway("unknown_quarantined"), Leases()
    result = await worker(store, gateway, leases).run_once()
    assert (result.released, result.pending) == (0, 1)
    assert (leases.calls, store.marks, store.deferrals) == (0, 0, 1)


@pytest.mark.asyncio
async def test_unavailable_gateway_keeps_capacity() -> None:
    store, gateway, leases = Store(), Gateway(None), Leases()
    result = await worker(store, gateway, leases).run_once()
    assert (result.released, result.pending, leases.calls, store.marks) == (0, 1, 0, 0)


@pytest.mark.asyncio
async def test_already_released_lease_only_gets_the_settled_marker() -> None:
    store, gateway, leases = Store(), Gateway("succeeded"), Leases(released=True)
    result = await worker(store, gateway, leases).run_once()
    assert (result.released, leases.calls, store.marks) == (1, 0, 1)


@pytest.mark.asyncio
async def test_no_submit_intent_goes_straight_to_the_shared_release_rule() -> None:
    store, gateway, leases = Store(intent=None), Gateway(None), Leases()
    result = await worker(store, gateway, leases).run_once()
    assert (result.released, leases.calls, gateway.queries, store.marks) == (1, 1, 0, 1)


@pytest.mark.asyncio
async def test_unknown_release_outcome_rereads_before_marking() -> None:
    store, gateway, leases = Store(), Gateway("succeeded"), Leases(commit_unknown=True)
    result = await worker(store, gateway, leases).run_once()
    assert (result.released, leases.calls, store.marks) == (1, 1, 1)


@pytest.mark.asyncio
async def test_route_mismatch_never_touches_the_lease() -> None:
    store, gateway, leases = Store(), Gateway("succeeded"), Leases()
    worker_ = AsrSettlementWorker(
        cast(LeaseService, leases),
        store,
        gateway,
        gateway_id="other-gateway",
        host_node_id="host-test",
    )
    result = await worker_.run_once()
    assert (result.released, leases.calls, store.marks, store.deferrals) == (0, 0, 0, 1)


@pytest.mark.asyncio
async def test_rejected_release_keeps_the_root_unsettled() -> None:
    class Refusing(Leases):
        def release(self, request: Any) -> LeaseResult:
            self.calls += 1
            return rejected(request.lease_id, "execution_cleanup_pending")

    store, gateway, leases = Store(), Gateway("succeeded"), Refusing()
    result = await worker(store, gateway, leases).run_once()
    assert (result.released, result.pending, store.marks, store.deferrals) == (0, 1, 0, 1)


def test_runtime_reraises_the_named_preflight_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speechnexus_backend import asr_settlement_worker as module

    settings = module.AsrSettlementWorkerSettings(
        database_url="postgresql://settlement.invalid/db",
        schema="speech",
        gateway_id="gateway-test",
        host_node_id="host-test",
        admission_database_url="postgresql://admission.invalid/db",
    )

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise SettlementPreflightUnavailableError(
            "run `docker compose --profile migration run --rm migrate` before starting"
        )

    monkeypatch.setattr(module, "run", fail)
    runtime = module.AsrSettlementRuntime(settings)
    with pytest.raises(SettlementPreflightUnavailableError, match="migrate"):
        runtime.open()
