"""Public Job cancellation on isolated PostgreSQL; no skip counts as evidence."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from tests.integration.test_public_jobs_http import PASSWORD, PREFIX, JobSandbox
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.app import create_app
from speechnexus_backend.job_cancellation_runtime import (
    JobCancellationRuntime,
    JobCancellationSettings,
)
from speechnexus_backend.speech.application.job_cancellation import JobCancellationService
from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    JobCancellationReconciler,
)
from speechnexus_backend.speech.application.public_jobs import (
    IdempotencyConflictError,
    JobCleanupPendingError,
)
from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.domain.public_jobs import PublicJobNotFoundError
from speechnexus_backend.speech.infrastructure.gateway_cancellation_confirmation import (
    GatewayCancellationConfirmer,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_reconciliation_postgres import (
    PostgresCancellationReconciliationStore,
)

pytestmark = pytest.mark.integration
CANCEL = PREFIX + "/{job_id}/cancellations"
KEY = "synthetic-cancel-key-1"


def _app(db: JobSandbox, confirmation: Any = None) -> Any:
    from tests.integration.test_public_jobs_http import _settings_for

    return create_app(settings=_settings_for(db), jobs=db.jobs, job_confirmation=confirmation)


class Stopper:
    def __init__(self, state: str) -> None:
        self.state = state
        self.calls = 0

    async def stop(self, job: Any, receipt: Any) -> Any:
        from speechnexus_backend.speech.application.job_cancellation import StopObservation

        self.calls += 1
        return StopObservation(cast(Any, self.state))


async def _create_running(db: JobSandbox, *, key: str, workspace_id: str | None = None) -> str:
    app = _app(db)
    body: dict[str, Any] = {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic cancel job"},
        "voice": {"id": "voice-vivian", "revision": "2"},
    }
    if workspace_id is not None:
        body["workspace_id"] = workspace_id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(PREFIX, json=body, headers={"Idempotency-Key": key})
        assert created.status_code == 202, created.text
    job_id = cast(str, created.json()["id"])
    db.jobs.advance(job_id, "running")
    return job_id


async def test_cancel_accepts_once_replays_and_rejects_key_reuse(jobs_db: JobSandbox) -> None:
    job_id = await _create_running(jobs_db, key="synthetic-cancel-create-1")
    app = _app(jobs_db)
    url = CANCEL.format(job_id=job_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(url, headers={"Idempotency-Key": KEY})
        assert first.status_code == 202, first.text
        assert first.headers["Retry-After"] == "1"
        receipt = first.json()
        assert receipt == {
            "job_id": job_id,
            "accepted": True,
            "status": "cancelling",
            "cleanup_state": "pending",
            "cancellation_requested_at": receipt["cancellation_requested_at"],
            "operations": [],
        }
        assert jobs_db.query("SELECT state FROM speech_jobs WHERE job_id = %s", (job_id,)) == [
            {"state": "cancelling"}
        ]
        assert jobs_db.query(
            "SELECT state FROM speech_job_events WHERE job_id = %s ORDER BY event_seq",
            (job_id,),
        ) == [{"state": "pending"}, {"state": "cancelling"}]
        assert jobs_db.count("speech_job_cancellations") == 1
        assert jobs_db.query(
            "SELECT f.job_id, f.accepted_at_us = c.requested_at_us AS same, "
            "f.request_id = c.request_id AS request_same "
            "FROM speech_job_cancellation_fences AS f "
            "JOIN speech_job_cancellations AS c ON c.job_id = f.job_id "
            "WHERE f.job_id = %s",
            (job_id,),
        ) == [{"job_id": job_id, "same": True, "request_same": True}]

        replay = await client.post(url, headers={"Idempotency-Key": KEY})
        assert replay.status_code == 202, replay.text
        assert replay.json() == receipt
        other_key = await client.post(url, headers={"Idempotency-Key": "synthetic-cancel-key-2"})
        assert other_key.status_code == 202, other_key.text
        assert other_key.json() == receipt
        assert jobs_db.count("speech_job_cancellations") == 1

        job = await client.get(PREFIX + "/" + job_id)
        assert job.status_code == 200, job.text
        assert job.json()["status"] == "cancelling"
        assert job.json()["cleanup_state"] == "pending"

    other = await _create_running(jobs_db, key="synthetic-cancel-create-2")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        conflict = await client.post(CANCEL.format(job_id=other), headers={"Idempotency-Key": KEY})
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert jobs_db.query("SELECT state FROM speech_jobs WHERE job_id = %s", (other,)) == [
        {"state": "running"}
    ]


def test_concurrent_cancel_persists_one_receipt(jobs_db: JobSandbox) -> None:
    job_id = asyncio.run(_create_running(jobs_db, key="synthetic-cancel-create-3"))

    def invoke(key: str) -> Any:
        return jobs_db.jobs.cancel(job_id, request_id="req-" + key, idempotency_key=key)

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(invoke, ("synthetic-concurrent-a", "synthetic-concurrent-b")))
    assert {outcome.accepted_now for outcome in outcomes} == {True, False}
    assert len({outcome.receipt.requested_at_us for outcome in outcomes}) == 1
    assert {outcome.job.state for outcome in outcomes} == {"cancelling"}
    assert jobs_db.count("speech_job_cancellations") == 1
    assert jobs_db.query(
        "SELECT count(*) AS n FROM speech_job_events WHERE job_id = %s AND state = 'cancelling'",
        (job_id,),
    ) == [{"n": 1}]


async def test_terminal_job_returns_its_receipt_without_reviving(jobs_db: JobSandbox) -> None:
    job_id = await _create_running(jobs_db, key="synthetic-cancel-create-4")
    jobs_db.jobs.advance(
        job_id,
        "succeeded",
        result_json='{"kind":"tts","audio_artifact_id":"artifact-synthetic","receipt_id":"receipt-synthetic"}',
    )
    app = _app(jobs_db)
    url = CANCEL.format(job_id=job_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        terminal = await client.post(url, headers={"Idempotency-Key": "synthetic-terminal-key"})
        assert terminal.status_code == 200, terminal.text
        assert terminal.json()["accepted"] is False
        assert terminal.json()["status"] == "succeeded"
        assert terminal.json()["cleanup_state"] == "complete"
        replay = await client.post(url, headers={"Idempotency-Key": "synthetic-terminal-key"})
        assert replay.status_code == 200, replay.text
        assert replay.json() == terminal.json()
    assert jobs_db.query("SELECT state FROM speech_jobs WHERE job_id = %s", (job_id,)) == [
        {"state": "succeeded"}
    ]
    assert jobs_db.count("speech_job_cancellations") == 1
    assert jobs_db.count("speech_job_cancellation_fences") == 0


async def test_cleanup_pending_terminal_is_not_reported_cancelled(jobs_db: JobSandbox) -> None:
    job_id = await _create_running(jobs_db, key="synthetic-cancel-create-5")
    jobs_db.jobs.advance(job_id, "failed", error_code="cancellation_unconfirmed")
    app = _app(jobs_db)
    url = CANCEL.format(job_id=job_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.post(url, headers={"Idempotency-Key": "synthetic-cleanup-key"})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "execution_cleanup_pending"
    row = jobs_db.query(
        "SELECT state, error_code, status_version FROM speech_jobs WHERE job_id = %s", (job_id,)
    )[0]
    assert row == {
        "state": "failed",
        "error_code": "cancellation_unconfirmed",
        "status_version": 3,
    }
    assert jobs_db.count("speech_job_cancellations") == 0


async def test_pending_job_can_be_cancelled_without_an_execution(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    body = {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic pending cancel"},
        "voice": {"id": "voice-vivian", "revision": "2"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            PREFIX, json=body, headers={"Idempotency-Key": "synthetic-pending-create"}
        )
        job_id = cast(str, created.json()["id"])
        cancelled = await client.post(
            CANCEL.format(job_id=job_id), headers={"Idempotency-Key": "synthetic-pending-key"}
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["cleanup_state"] == "complete"
        assert cancelled.json()["accepted"] is True
        replay = await client.post(
            CANCEL.format(job_id=job_id), headers={"Idempotency-Key": "synthetic-pending-key"}
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == cancelled.json()
    assert jobs_db.count("speech_job_cancellations") == 1


async def test_verified_stop_settles_cancelled(jobs_db: JobSandbox) -> None:
    job_id = await _create_running(jobs_db, key="synthetic-cancel-create-6")
    stopper = Stopper("stopped")
    app = _app(jobs_db, JobCancellationService(jobs_db.jobs, stopper))
    url = CANCEL.format(job_id=job_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cancelled = await client.post(url, headers={"Idempotency-Key": "synthetic-stopped-key"})
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["cleanup_state"] == "complete"
        assert cancelled.json()["accepted"] is True
        again = await client.post(url, headers={"Idempotency-Key": "synthetic-stopped-key"})
        assert again.status_code == 200, again.text
        assert again.json() == cancelled.json()
        job = await client.get(PREFIX + "/" + job_id)
        assert job.json()["status"] == "cancelled"
    assert stopper.calls == 1
    assert jobs_db.query(
        "SELECT state, status_version FROM speech_jobs WHERE job_id = %s", (job_id,)
    ) == [{"state": "cancelled", "status_version": 4}]
    assert jobs_db.query(
        "SELECT state FROM speech_job_events WHERE job_id = %s ORDER BY event_seq", (job_id,)
    ) == [{"state": "pending"}, {"state": "cancelling"}, {"state": "cancelled"}]
    assert jobs_db.count("speech_job_cancellations") == 1


async def test_unconfirmed_or_missing_execution_stays_cleanup_pending(
    jobs_db: JobSandbox,
) -> None:
    for index, state in enumerate(("unconfirmed", "not_found", "failed")):
        job_id = await _create_running(jobs_db, key=f"synthetic-cancel-unconfirmed-{index}")
        app = _app(jobs_db, JobCancellationService(jobs_db.jobs, Stopper(state)))
        url = CANCEL.format(job_id=job_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            pending = await client.post(
                url, headers={"Idempotency-Key": f"synthetic-unconfirmed-key-{index}"}
            )
            assert pending.status_code == 202, pending.text
            assert pending.json()["status"] == "cancelling"
            assert pending.json()["cleanup_state"] == "pending"
            assert pending.json()["accepted"] is True
        assert jobs_db.query("SELECT state FROM speech_jobs WHERE job_id = %s", (job_id,)) == [
            {"state": "cancelling"}
        ]


async def test_accepted_cancellation_reconciles_from_no_execution_and_restart_converges(
    jobs_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = await _create_running(jobs_db, key="synthetic-e2e-create-1")
    app = _app(jobs_db)
    url = CANCEL.format(job_id=job_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.post(url, headers={"Idempotency-Key": "synthetic-e2e-key-1"})
        assert accepted.status_code == 202, accepted.text
    assert jobs_db.count("speech_job_cancellations") == 1
    assert jobs_db.count("speech_job_cancellation_fences") == 1

    submit_id = submit_intent_identity(job_id)
    document = {
        "submit_intent_id": submit_id,
        "operation_id": job_id,
        "attempt_id": job_id + "-a1",
        "execution_state": "failed",
        "cleanup_state": "not_required",
        "error_code": "gateway_barrier_fenced",
        "stop_evidence_kind": "none",
        "dispatched_at": None,
        "engine_request_id": None,
    }

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def query_document(self, submit_intent_id: str) -> dict[str, Any]:
            assert submit_intent_id == submit_id
            return document

    monkeypatch.setattr(
        GatewayExecutionClient,
        "from_settings",
        classmethod(lambda cls, settings: Client()),
    )

    class Settlement:
        def settle_cancelled(self, target: str) -> bool:
            current = jobs_db.jobs.get(target)
            if current.state == "cancelled":
                return True
            if current.state != "cancelling":
                return False
            return (
                jobs_db.jobs.settle_cancelled(target, expected_version=current.status_version).state
                == "cancelled"
            )

    confirmer = GatewayCancellationConfirmer(cast(Any, object()), Settlement())
    reconciler = JobCancellationReconciler(
        store=PostgresCancellationReconciliationStore(jobs_db.pool),
        confirmer=confirmer,
        min_age_seconds=0,
        max_attempts=3,
    )
    batch = await reconciler.run_once(now_us=time.time_ns() // 1_000 + 1_000_000)
    assert batch.claimed == 1 and batch.settled == 1
    assert jobs_db.query("SELECT state FROM speech_jobs WHERE job_id = %s", (job_id,)) == [
        {"state": "cancelled"}
    ]

    restarted = JobCancellationReconciler(
        store=PostgresCancellationReconciliationStore(jobs_db.pool),
        confirmer=confirmer,
        min_age_seconds=0,
        max_attempts=3,
    )
    assert (await restarted.run_once(now_us=time.time_ns() // 1_000 + 2_000_000)).claimed == 0
    assert jobs_db.query("SELECT state FROM speech_jobs WHERE job_id = %s", (job_id,)) == [
        {"state": "cancelled"}
    ]


async def test_composed_runtime_settles_and_restart_converges(
    jobs_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = await _create_running(jobs_db, key="synthetic-runtime-create-1")
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.post(
            CANCEL.format(job_id=job_id), headers={"Idempotency-Key": "synthetic-runtime-key-1"}
        )
        assert accepted.status_code == 202, accepted.text

    submit_id = submit_intent_identity(job_id)
    document = {
        "submit_intent_id": submit_id,
        "operation_id": job_id,
        "attempt_id": job_id + "-a1",
        "execution_state": "failed",
        "cleanup_state": "not_required",
        "error_code": "gateway_barrier_fenced",
        "stop_evidence_kind": "none",
        "dispatched_at": None,
        "engine_request_id": None,
    }

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def query_document(self, target: str) -> dict[str, Any]:
            assert target == submit_id
            return document

    monkeypatch.setattr(
        GatewayExecutionClient,
        "from_settings",
        classmethod(lambda cls, settings: Client()),
    )
    settings = JobCancellationSettings(
        enabled=True,
        database_url=make_conninfo(jobs_db.url, user=jobs_db.role, password=PASSWORD),
        database_schema=jobs_db.schema,
        gateway_base_url="http://127.0.0.1:8010",
        gateway_credential="synthetic-token-0001",
        interval_seconds=1,
        min_age_seconds=0,
        max_attempts=3,
    )
    runtime = JobCancellationRuntime(settings)
    await runtime.open()
    try:
        batch = await runtime.run_once()
    finally:
        await runtime.close()
    assert batch.claimed == 1 and batch.settled == 1
    assert jobs_db.query("SELECT state FROM speech_jobs WHERE job_id = %s", (job_id,)) == [
        {"state": "cancelled"}
    ]

    restarted = JobCancellationRuntime(settings)
    await restarted.open()
    try:
        second = await restarted.run_once()
    finally:
        await restarted.close()
    assert second.claimed == 0


async def test_workspace_scope_rejects_foreign_job_and_isolates_keys(
    jobs_db: JobSandbox,
) -> None:
    jobs_db.admin.execute(
        "INSERT INTO speech_workspaces "
        "(workspace_id, display_name, workspace_state, created_at_us) "
        "VALUES ('workspace-other', 'Synthetic other', 'active', 1)"
    )
    first = await _create_running(jobs_db, key="synthetic-workspace-create-1")
    second = await _create_running(
        jobs_db, key="synthetic-workspace-create-2", workspace_id="workspace-other"
    )
    with pytest.raises(PublicJobNotFoundError):
        jobs_db.jobs.cancel(
            first,
            workspace_id="workspace-other",
            request_id="req-foreign",
            idempotency_key="synthetic-foreign-key",
        )
    accepted = jobs_db.jobs.cancel(
        first, request_id="req-first", idempotency_key="synthetic-shared-key"
    )
    other = jobs_db.jobs.cancel(
        second, request_id="req-second", idempotency_key="synthetic-shared-key"
    )
    assert accepted.job.job_id == first
    assert other.job.job_id == second
    assert accepted.receipt.job_id != other.receipt.job_id
    assert jobs_db.count("speech_job_cancellations") == 2


async def test_unknown_and_foreign_job_are_not_found(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post(
            CANCEL.format(job_id="job-00000000000000000000000000000000"),
            headers={"Idempotency-Key": "synthetic-missing-key"},
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["error"]["code"] == "job_not_found"
        invalid = await client.post(
            CANCEL.format(job_id="NOT_VALID"), headers={"Idempotency-Key": "synthetic-invalid-key"}
        )
        assert invalid.status_code == 404, invalid.text
        bad_key = await client.post(
            CANCEL.format(job_id="job-00000000000000000000000000000000"),
            headers={"Idempotency-Key": "short"},
        )
        assert bad_key.status_code == 400, bad_key.text


def test_cleanup_pending_error_is_typed(jobs_db: JobSandbox) -> None:
    # The application-level guard is the same one the route maps to 409.
    assert issubclass(JobCleanupPendingError, RuntimeError)
    assert issubclass(IdempotencyConflictError, RuntimeError)
