"""Bounded, restart-safe cancellation reconciliation without a real engine."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    CancellationCandidate,
    CancellationReconciliationWorker,
    Confirmation,
    JobCancellationReconciler,
)
from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.infrastructure.gateway_cancellation_confirmation import (
    GatewayCancellationConfirmer,
    not_started,
    verified_stop,
)

NOW = 1_700_000_000_000_000


class FakeStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {row["job_id"]: dict(row) for row in rows}
        self.claims: list[str] = []

    def claim_due(
        self,
        *,
        now_us: int,
        limit: int,
        lease_until_us: int,
        accepted_before_us: int,
    ) -> list[CancellationCandidate]:
        due = [
            row
            for row in self.rows.values()
            if row["settled_at_us"] is None
            and row["last_outcome"] != "exhausted"
            and row["next_check_at_us"] <= now_us
            and row["accepted_at_us"] <= accepted_before_us
        ]
        due.sort(key=lambda row: (row["accepted_at_us"], row["job_id"]))
        result = []
        for row in due[:limit]:
            row["attempts"] += 1
            row["next_check_at_us"] = lease_until_us
            self.claims.append(row["job_id"])
            result.append(
                CancellationCandidate(
                    row["job_id"],
                    row["accepted_at_us"],
                    row["attempts"],
                    row["next_check_at_us"],
                )
            )
        return result

    def record_settled(self, job_id: str, *, now_us: int) -> None:
        row = self.rows[job_id]
        row["settled_at_us"] = now_us
        row["last_outcome"] = "settled"
        row["next_check_at_us"] = now_us

    def record_retry(
        self,
        job_id: str,
        *,
        now_us: int,
        outcome: str,
        next_check_at_us: int,
    ) -> None:
        row = self.rows[job_id]
        row["last_outcome"] = outcome
        row["next_check_at_us"] = next_check_at_us

    def record_exhausted(self, job_id: str, *, now_us: int, outcome: str) -> None:
        row = self.rows[job_id]
        row["last_outcome"] = outcome
        row["next_check_at_us"] = now_us


class SequenceConfirmer:
    def __init__(self, *outcomes: str) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    async def confirm(self, job_id: str) -> Confirmation:
        self.calls.append(job_id)
        outcome = self.outcomes.pop(0) if self.outcomes else "pending"
        return Confirmation(cast(Any, outcome))


def reconciler(
    store: FakeStore,
    confirmer: SequenceConfirmer,
    *,
    max_attempts: int = 3,
    base_backoff_seconds: float = 1,
    max_backoff_seconds: float = 4,
    batch_size: int = 8,
) -> JobCancellationReconciler:
    return JobCancellationReconciler(
        store=store,
        confirmer=confirmer,
        max_attempts=max_attempts,
        batch_size=batch_size,
        lease_seconds=10,
        base_backoff_seconds=base_backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
        budget_seconds=2,
        min_age_seconds=0,
    )


def row(job_id: str = "job-fence-1") -> dict[str, Any]:
    return {
        "job_id": job_id,
        "accepted_at_us": NOW,
        "attempts": 0,
        "next_check_at_us": NOW,
        "last_outcome": None,
        "settled_at_us": None,
    }


def test_settled_job_is_never_claimed_again() -> None:
    store = FakeStore([row()])
    confirmer = SequenceConfirmer("settled")
    worker = reconciler(store, confirmer)

    first = asyncio.run(worker.run_once(now_us=NOW))
    second = asyncio.run(worker.run_once(now_us=NOW + 10_000_000))

    assert (first.claimed, first.settled) == (1, 1)
    assert (second.claimed, second.settled) == (0, 0)
    assert store.rows["job-fence-1"]["settled_at_us"] == NOW
    assert confirmer.calls == ["job-fence-1"]


def test_retry_uses_bounded_exponential_backoff() -> None:
    store = FakeStore([row()])
    confirmer = SequenceConfirmer("pending", "pending", "pending")
    worker = reconciler(store, confirmer, max_attempts=5)

    asyncio.run(worker.run_once(now_us=NOW))
    assert store.rows["job-fence-1"]["next_check_at_us"] == NOW + 1_000_000
    assert asyncio.run(worker.run_once(now_us=NOW + 500_000)).claimed == 0
    asyncio.run(worker.run_once(now_us=NOW + 1_000_000))
    assert store.rows["job-fence-1"]["attempts"] == 2
    assert store.rows["job-fence-1"]["next_check_at_us"] == NOW + 3_000_000
    asyncio.run(worker.run_once(now_us=NOW + 3_000_000))
    assert store.rows["job-fence-1"]["attempts"] == 3
    assert store.rows["job-fence-1"]["next_check_at_us"] == NOW + 7_000_000
    asyncio.run(worker.run_once(now_us=NOW + 7_000_000))
    assert store.rows["job-fence-1"]["next_check_at_us"] == NOW + 11_000_000


def test_attempt_budget_exhausts_without_infinite_retry() -> None:
    store = FakeStore([row()])
    confirmer = SequenceConfirmer("pending", "pending", "pending", "pending")
    worker = reconciler(store, confirmer, max_attempts=2)

    asyncio.run(worker.run_once(now_us=NOW))
    asyncio.run(worker.run_once(now_us=NOW + 1_000_000))
    third = asyncio.run(worker.run_once(now_us=NOW + 3_000_000))
    fourth = asyncio.run(worker.run_once(now_us=NOW + 60_000_000))

    assert (third.claimed, third.exhausted) == (1, 1)
    assert fourth.claimed == 0
    assert store.rows["job-fence-1"]["last_outcome"] == "exhausted"
    assert confirmer.calls == ["job-fence-1", "job-fence-1"]


def test_restart_resumes_from_persisted_schedule() -> None:
    store = FakeStore([row()])
    first = reconciler(store, SequenceConfirmer("pending"))
    asyncio.run(first.run_once(now_us=NOW))
    assert store.rows["job-fence-1"]["attempts"] == 1

    restarted = reconciler(store, SequenceConfirmer("settled"))
    outcome = asyncio.run(restarted.run_once(now_us=NOW + 1_000_000))

    assert (outcome.claimed, outcome.settled) == (1, 1)
    assert store.rows["job-fence-1"]["attempts"] == 2


def test_terminal_unconfirmed_is_exhausted_without_settlement() -> None:
    store = FakeStore([row()])
    confirmer = SequenceConfirmer("terminal_unconfirmed")
    worker = reconciler(store, confirmer)

    outcome = asyncio.run(worker.run_once(now_us=NOW))

    assert (outcome.exhausted, outcome.settled) == (1, 0)
    assert store.rows["job-fence-1"]["last_outcome"] == "terminal_unconfirmed"
    assert store.rows["job-fence-1"]["settled_at_us"] is None


def test_confirmer_failure_is_retried_as_unavailable() -> None:
    class Broken:
        async def confirm(self, job_id: str) -> Confirmation:
            raise RuntimeError("transport")

    store = FakeStore([row()])
    worker = reconciler(store, cast(Any, Broken()))

    outcome = asyncio.run(worker.run_once(now_us=NOW))

    assert (outcome.retried, outcome.unavailable) == (1, 1)
    assert store.rows["job-fence-1"]["last_outcome"] == "unavailable"


def test_batch_and_claim_order_are_bounded() -> None:
    store = FakeStore([row("job-fence-3"), row("job-fence-1"), row("job-fence-2")])
    confirmer = SequenceConfirmer("pending", "pending")
    worker = reconciler(store, confirmer, batch_size=2)

    outcome = asyncio.run(worker.run_once(now_us=NOW))

    assert outcome.claimed == 2
    assert confirmer.calls == ["job-fence-1", "job-fence-2"]


class FlakyReconciler:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self, *, now_us: int) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary store outage")
        return None


def test_periodic_worker_survives_a_store_error_and_stops() -> None:
    reconciler = FlakyReconciler()
    errors: list[BaseException] = []
    worker = CancellationReconciliationWorker(
        cast(Any, reconciler), interval_seconds=0.01, on_error=errors.append
    )

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        for _ in range(100):
            if reconciler.calls >= 3:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())

    assert reconciler.calls >= 3
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_periodic_worker_does_not_swallow_errors_without_a_reporter() -> None:
    class Broken:
        async def run_once(self, *, now_us: int) -> Any:
            raise RuntimeError("store")

    worker = CancellationReconciliationWorker(cast(Any, Broken()), interval_seconds=0.01)
    with pytest.raises(RuntimeError):
        asyncio.run(worker.run(asyncio.Event()))


class FakeClient:
    def __init__(self, document: dict[str, Any] | Exception) -> None:
        self.document = document

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def query_document(self, submit_intent_id: str) -> dict[str, Any]:
        if isinstance(self.document, Exception):
            raise self.document
        return self.document


class FakeSettlement:
    def __init__(self, settled: bool = True) -> None:
        self.settled = settled
        self.calls: list[str] = []

    def settle_cancelled(self, job_id: str) -> bool:
        self.calls.append(job_id)
        return self.settled


def document(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "submit_intent_id": submit_intent_identity("job-fence-1"),
        "operation_id": "job-fence-1",
        "execution_state": "failed",
        "cleanup_state": "not_required",
        "error_code": "gateway_barrier_fenced",
        "stop_evidence_kind": "none",
        "dispatched_at": None,
        "engine_request_id": None,
    }
    base.update(overrides)
    return base


def confirmer_for(
    monkeypatch: pytest.MonkeyPatch,
    doc: dict[str, Any] | Exception,
    settlement: FakeSettlement,
    *,
    stop: Any = None,
) -> GatewayCancellationConfirmer:
    from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
        GatewayExecutionClient,
    )

    monkeypatch.setattr(
        GatewayExecutionClient,
        "from_settings",
        classmethod(lambda cls, settings: FakeClient(doc)),
    )
    return GatewayCancellationConfirmer(cast(Any, object()), settlement, stop_attempt=stop)


def test_no_execution_fact_settles_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    settlement = FakeSettlement()
    confirmer = confirmer_for(monkeypatch, document(), settlement)
    result = asyncio.run(confirmer.confirm("job-fence-1"))

    assert result.outcome == "settled"
    assert settlement.calls == ["job-fence-1"]
    assert document()["submit_intent_id"] == submit_intent_identity("job-fence-1")


def test_verified_stop_settles_but_aborted_without_evidence_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = document(
        execution_state="aborted",
        cleanup_state="complete",
        stop_evidence_kind="engine_abort_ack",
        error_code=None,
    )
    settlement = FakeSettlement()
    confirmer = confirmer_for(monkeypatch, stopped, settlement)
    assert asyncio.run(confirmer.confirm("job-fence-1")).outcome == "settled"

    bare = document(
        execution_state="aborted",
        cleanup_state="complete",
        stop_evidence_kind="none",
        error_code=None,
    )
    settlement = FakeSettlement()
    confirmer = confirmer_for(monkeypatch, bare, settlement)
    assert asyncio.run(confirmer.confirm("job-fence-1")).outcome == "stop_unconfirmed"
    assert settlement.calls == []


def test_failed_with_dispatch_is_not_no_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched = document(dispatched_at="2026-10-24T00:00:00Z", engine_request_id="engine-1")
    settlement = FakeSettlement()
    confirmer = confirmer_for(monkeypatch, dispatched, settlement)

    result = asyncio.run(confirmer.confirm("job-fence-1"))

    assert result.outcome == "stop_unconfirmed"
    assert settlement.calls == []
    assert not not_started(dispatched)


def test_active_execution_uses_one_bounded_stop_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = document(execution_state="executing", cleanup_state="pending", error_code=None)
    settlement = FakeSettlement()
    attempts: list[str] = []

    async def stop(job_id: str, observed: Any) -> bool:
        attempts.append(job_id)
        return True

    confirmer = confirmer_for(monkeypatch, active, settlement, stop=stop)
    result = asyncio.run(confirmer.confirm("job-fence-1"))

    assert result.outcome == "settled"
    assert attempts == ["job-fence-1"]

    settlement = FakeSettlement()
    confirmer = confirmer_for(monkeypatch, active, settlement)
    assert asyncio.run(confirmer.confirm("job-fence-1")).outcome == "pending"


def test_missing_intent_is_not_stop_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
        GatewayRejectedError,
    )

    settlement = FakeSettlement()
    confirmer = confirmer_for(
        monkeypatch,
        GatewayRejectedError("missing", request_may_have_run=False, status_code=404),
        settlement,
    )

    result = asyncio.run(confirmer.confirm("job-fence-1"))

    assert result.outcome == "pending"
    assert settlement.calls == []


def test_completed_execution_is_terminal_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = document(
        execution_state="succeeded",
        cleanup_state="complete",
        error_code=None,
        stop_evidence_kind="engine_terminal_response",
        dispatched_at="2026-10-24T00:00:00Z",
    )
    settlement = FakeSettlement()
    confirmer = confirmer_for(monkeypatch, completed, settlement)

    assert asyncio.run(confirmer.confirm("job-fence-1")).outcome == "terminal_unconfirmed"
    assert settlement.calls == []


def test_identity_mismatch_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    settlement = FakeSettlement()
    confirmer = confirmer_for(monkeypatch, document(operation_id="another-job"), settlement)

    assert asyncio.run(confirmer.confirm("job-fence-1")).outcome == "unavailable"
    assert settlement.calls == []


def test_predicates_require_exact_no_execution_and_stop_evidence() -> None:
    assert not_started(document())
    assert not not_started(document(dispatched_at="2026-10-24T00:00:00Z"))
    assert verified_stop(
        document(
            execution_state="aborted",
            cleanup_state="complete",
            stop_evidence_kind="operator_verified_instance_terminated",
            error_code=None,
        )
    )
    assert not verified_stop(
        document(execution_state="aborted", cleanup_state="pending", error_code=None)
    )
