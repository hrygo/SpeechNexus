"""Real periodic thread over the native recovery service; no database or model."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from speechnexus_gateway.app import _ControlReadinessProbe, create_app
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.recover_executions import ExecutionRecoveryService
from speechnexus_gateway.domain.recovery import InstanceTerminationEvidence
from speechnexus_gateway.domain.submission import ExecutionState, SubmissionIntent
from speechnexus_gateway.infrastructure.recovery_worker import RecoveryWorker
from tests.fake.execution import fake_ingress
from tests.unit.gateway.test_execution_recovery_sweep import (
    ScriptedTerminations,
    SpyLedger,
    identity_for,
    quarantined,
    termination_evidence,
)
from tests.unit.gateway.test_recovery_configuration import configured


class WaitingSource:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.entered, self.release, self.next_entered = Event(), Event(), Event()
        self.calls: list[str] = []
        self.fail_first = fail_first

    def termination_evidence(self, intent: SubmissionIntent) -> InstanceTerminationEvidence | None:
        self.calls.append(intent.identity.submit_intent_id)
        if len(self.calls) == 1:
            self.entered.set()
            assert self.release.wait(3), "the test must release its blocked source"
            if self.fail_first:
                raise ValueError("private-sensitive-credential")
        else:
            self.next_entered.set()
        return termination_evidence()


def service_for(
    ledger: SpyLedger, source: WaitingSource | ScriptedTerminations
) -> ExecutionRecoveryService:
    ingress = fake_ingress(node_id="node-1", gateway_id="gateway-1")
    return ExecutionRecoveryService(
        ingress=ingress,
        ledger=ledger,
        submissions=ExecutionQueryService(ingress=ingress, ledger=ledger),
        terminations=source,
    )


def test_stop_hook_does_not_visit_or_advance_unexamined_rows() -> None:
    ledger = SpyLedger()
    quarantined(ledger, identity=identity_for(submit_intent_id="intent-a"))
    quarantined(ledger, identity=identity_for(submit_intent_id="intent-b"))
    source = ScriptedTerminations()
    recovery = service_for(ledger, source)
    assert recovery.sweep(should_stop=lambda: True).examined == 0
    assert not source.calls
    report = recovery.sweep(should_stop=lambda: len(source.calls) == 1)
    assert report.examined == report.still_unknown == 1
    assert source.calls == ["intent-a"]
    assert recovery.sweep().examined == 1
    assert source.calls == ["intent-a", "intent-b"]


def test_close_joins_current_call_and_stops_before_the_next_row() -> None:
    ledger = SpyLedger()
    quarantined(ledger, identity=identity_for(submit_intent_id="intent-a"))
    quarantined(ledger, identity=identity_for(submit_intent_id="intent-b"))
    source = WaitingSource()
    worker = RecoveryWorker(
        service_for(ledger, source), interval_seconds=3600, batch_size=2, pass_budget_seconds=10
    )
    worker.start()
    try:
        assert source.entered.wait(2) and bool(worker.running)
        with ThreadPoolExecutor(max_workers=1) as threads:
            closing = threads.submit(worker.close)
            deadline = monotonic() + 2
            while worker.running and monotonic() < deadline:
                Event().wait(0.001)
            assert not worker.running
            assert not closing.done()
            source.release.set()
            closing.result(2)
        assert not worker.running and source.calls == ["intent-a"]
        assert ledger.get("intent-a").execution_state is ExecutionState.ABORTED
        assert ledger.get("intent-b").execution_state is ExecutionState.UNKNOWN_QUARANTINED
        worker.close()
        with pytest.raises(RuntimeError, match="closed recovery worker"):
            worker.start()
    finally:
        source.release.set()
        worker.close()


def test_failed_pass_automatically_visits_next_page_without_sensitive_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = SpyLedger()
    quarantined(ledger, identity=identity_for(submit_intent_id="intent-a"))
    quarantined(ledger, identity=identity_for(submit_intent_id="intent-b"))
    source = WaitingSource(fail_first=True)
    worker = RecoveryWorker(
        service_for(ledger, source), interval_seconds=0.01, batch_size=1, pass_budget_seconds=1
    )
    worker.start()
    try:
        assert source.entered.wait(2)
        source.release.set()
        assert source.next_entered.wait(2)
    finally:
        worker.close()
    assert source.calls[:2] == ["intent-a", "intent-b"]
    assert "gateway recovery pass failed" in caplog.text
    assert "private-sensitive" not in caplog.text


def test_pass_budget_finishes_active_call_but_does_not_start_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = SpyLedger()
    quarantined(ledger, identity=identity_for(submit_intent_id="intent-a"))
    quarantined(ledger, identity=identity_for(submit_intent_id="intent-b"))
    source = WaitingSource()
    ticks = [0.0]
    expired = Event()

    def clock() -> float:
        if ticks[0] >= 2:
            expired.set()
        return ticks[0]

    monkeypatch.setattr("speechnexus_gateway.infrastructure.recovery_worker.monotonic", clock)
    worker = RecoveryWorker(
        service_for(ledger, source), interval_seconds=3600, batch_size=2, pass_budget_seconds=1
    )
    worker.start()
    try:
        assert source.entered.wait(2)
        ticks[0] = 2
        source.release.set()
        assert expired.wait(2)  # The pass itself checks the deadline before close is called.
        # close joins the active call; no timeout/cancellation may fabricate its outcome.
        worker.close()
        assert source.calls == ["intent-a"]
        assert ledger.get("intent-a").execution_state is ExecutionState.ABORTED
        assert ledger.get("intent-b").execution_state is ExecutionState.UNKNOWN_QUARANTINED
    finally:
        source.release.set()
        worker.close()


def app_with_waiting_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FastAPI, RecoveryWorker, WaitingSource, list[str], Event]:
    ledger = SpyLedger()
    quarantined(ledger)
    source = WaitingSource()
    ingress = fake_ingress(node_id="node-1", gateway_id="gateway-1")
    queries = ExecutionQueryService(ingress=ingress, ledger=ledger)
    barrier = GatewayBarrierService(ingress=ingress, ledger=ledger)
    drain = InstanceDrainService(ingress=ingress, ledger=ledger)
    worker = RecoveryWorker(
        service_for(ledger, source), interval_seconds=3600, batch_size=1, pass_budget_seconds=10
    )
    closed: list[str] = []
    resources_closed = Event()

    def close_ownership() -> None:
        closed.append("ownership")
        resources_closed.set()

    monkeypatch.setattr(
        "speechnexus_gateway.app._build_control",
        lambda _: (
            barrier,
            queries,
            drain,
            SimpleNamespace(close=close_ownership),
            SimpleNamespace(close=lambda: closed.append("pool")),
            worker,
        ),
    )
    app = create_app(settings=configured(tmp_path, tmp_path / "unused.json"))
    return app, worker, source, closed, resources_closed


async def test_lifespan_keeps_pool_and_ownership_until_worker_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, worker, source, closed, _ = app_with_waiting_worker(tmp_path, monkeypatch)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        assert await asyncio.to_thread(source.entered.wait, 2)
        await _ControlReadinessProbe(app).check()
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        assert await asyncio.to_thread(worker._stop.wait, 2)
        assert not worker.running and not shutdown.done() and not closed
        source.release.set()
        await asyncio.wait_for(shutdown, 2)
    finally:
        source.release.set()
        worker.close()
    assert closed == ["pool", "ownership"]
    assert app.state.recovery_worker is app.state.execution_query is None
    with pytest.raises(RuntimeError, match="control is not running"):
        await _ControlReadinessProbe(app).check()


async def test_cancelled_lifespan_cannot_close_resources_before_worker_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, worker, source, closed, resources_closed = app_with_waiting_worker(tmp_path, monkeypatch)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        assert await asyncio.to_thread(source.entered.wait, 2)
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        assert await asyncio.to_thread(worker._stop.wait, 2)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done() and not closed
        shutdown.cancel()  # Repeated cancellation must not abandon cleanup either.
        await asyncio.sleep(0)
        assert not shutdown.done() and not closed
        source.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, 2)
        assert await asyncio.to_thread(resources_closed.wait, 2)
        assert closed == ["pool", "ownership"]
    finally:
        source.release.set()
        worker.close()


@pytest.mark.parametrize("running", [None, False])
async def test_readiness_refuses_a_missing_or_dead_configured_worker(
    tmp_path: Path, running: bool | None
) -> None:
    ingress, ledger = fake_ingress(), SpyLedger()
    app = create_app(settings=configured(tmp_path, tmp_path / "unused.json"))
    app.state.barrier_control = GatewayBarrierService(ingress=ingress, ledger=ledger)
    app.state.execution_query = ExecutionQueryService(ingress=ingress, ledger=ledger)
    if running is False:
        app.state.recovery_worker = RecoveryWorker(
            service_for(ledger, ScriptedTerminations()),
            interval_seconds=1,
            batch_size=1,
            pass_budget_seconds=1,
        )
    with pytest.raises(RuntimeError, match="recovery is not running"):
        await _ControlReadinessProbe(app).check()
