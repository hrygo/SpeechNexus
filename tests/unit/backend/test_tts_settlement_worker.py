"""Settlement preserves original identity and requires authoritative lease release."""

from __future__ import annotations

from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.application.tts_settlement import TtsSettlementWorker
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.leases import LeaseRecord, LeaseResult, rejected, unknown
from speechnexus_backend.speech.domain.tts_settlement import SettlementRoot, TtsSettlementTarget
from speechnexus_backend.speech.infrastructure.tts_submit import build_tts_submit
from tests.unit.backend.test_tts_durable_intent import execution, plan


def target() -> TtsSettlementTarget:
    request = cast(dict[str, Any], build_tts_submit(plan(), execution()))
    return TtsSettlementTarget(
        **{key: request[key] for key in TtsSettlementTarget.__dataclass_fields__}
    )


def lease(state: str = "executing") -> LeaseRecord:
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
    def __init__(self, *, deadline: int = 100, reason: Any = None) -> None:
        self.root = SettlementRoot(target(), deadline, 50, reason)
        self.reason = reason
        self.marks = 0
        self.deferrals = 0

    def due(self, gateway_id: str, node_id: str, limit: int) -> list[SettlementRoot]:
        return [self.root]

    def request_abort(self, lease_id: str, reason: Any) -> Any:
        self.reason = self.reason or reason
        return self.reason

    def defer(self, lease_id: str, *, seconds: float) -> None:
        self.deferrals += 1

    def mark_settled(self, lease_id: str) -> None:
        self.marks += 1


class Leases:
    def __init__(self, *, released: bool = False, commit_unknown: bool = False) -> None:
        self.current = lease("released" if released else "executing")
        self.calls = 0
        self.commit_unknown = commit_unknown

    def query(self, lease_id: str) -> LeaseResult:
        return LeaseResult("replayed", lease_id, self.current)

    def release(self, request: Any) -> LeaseResult:
        self.calls += 1
        if self.commit_unknown:
            self.current = lease("released")
            return unknown(request.lease_id)
        return rejected(request.lease_id, "execution_cleanup_pending")

    def quarantine(self, request: Any) -> LeaseResult:
        return rejected(request.lease_id, "execution_cleanup_pending")


class Gateway:
    def __init__(self, state: str | None) -> None:
        self.state = state
        self.aborts: list[tuple[TtsSettlementTarget, Any]] = []

    async def query_tts_target(self, value: TtsSettlementTarget) -> Any:
        if self.state is None:
            raise OSError("synthetic unavailable")
        return {"execution_state": self.state}

    async def abort_tts(
        self, value: TtsSettlementTarget, *, reason: Any, wait_timeout_ms: int
    ) -> Any:
        self.aborts.append((value, reason))
        raise TimeoutError


def worker(store: Store, gateway: Gateway, leases: Leases) -> TtsSettlementWorker:
    return TtsSettlementWorker(
        store, gateway, cast(LeaseService, leases), gateway_id="gateway-test", node_id="host-test"
    )


@pytest.mark.asyncio
async def test_success_receipt_does_not_release_without_stop_evidence() -> None:
    store, gateway, leases = Store(), Gateway("succeeded"), Leases()
    result = await worker(store, gateway, leases).run_once()
    assert (result.released, result.pending, leases.calls, store.marks) == (0, 1, 1, 0)


@pytest.mark.asyncio
async def test_absence_before_deadline_never_releases() -> None:
    store, gateway, leases = Store(), Gateway(None), Leases()
    await worker(store, gateway, leases).run_once()
    assert leases.calls == 0 and gateway.aborts == [] and store.deferrals == 1


@pytest.mark.asyncio
async def test_deadline_persists_original_reason_before_abort_and_replays_it() -> None:
    store, gateway, leases = Store(deadline=40), Gateway("executing"), Leases()
    await worker(store, gateway, leases).run_once()
    assert store.reason == "deadline_exceeded"
    assert gateway.aborts == [(target(), "deadline_exceeded")]
    assert store.marks == 0


@pytest.mark.asyncio
async def test_unknown_release_queries_original_before_marking_settled() -> None:
    store, gateway, leases = Store(), Gateway("succeeded"), Leases(commit_unknown=True)
    result = await worker(store, gateway, leases).run_once()
    assert (result.released, leases.calls, store.marks) == (1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["http://remote.example", "https://user:secret@example.test"])
async def test_worker_rejects_untrusted_gateway_before_opening_pools(origin: str) -> None:
    from speechnexus_backend.tts_settlement_worker import TtsSettlementWorkerSettings, run

    settings = TtsSettlementWorkerSettings.model_validate(
        {
            "SPEECHNEXUS_DATABASE_URL": "postgresql://synthetic@127.0.0.1/test",
            "SPEECHNEXUS_TTS_SETTLEMENT_DATABASE_URL": "postgresql://worker@127.0.0.1/test",
            "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
            "SPEECHNEXUS_TTS_GATEWAY_ID": "gateway-test",
            "SPEECHNEXUS_TTS_HOST_NODE_ID": "host-test",
            "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": origin,
            "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "synthetic-test-token",
        }
    )
    with pytest.raises(ValueError, match="safe origin"):
        await run(settings)
