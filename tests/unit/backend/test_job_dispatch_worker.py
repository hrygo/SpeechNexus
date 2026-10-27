"""Entry-point and lease-renewal behavior without a real engine or object store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic import SecretStr, ValidationError

from speechnexus_backend.job_dispatch_runtime import (
    DispatchDependenciesUnavailableError,
    JobDispatchSettings,
    build_dispatch_dependencies,
)
from speechnexus_backend.job_dispatch_worker import (
    BATCH_LOG_FIELDS,
    batch_log,
    main,
    run_dispatch_worker,
)
from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher
from speechnexus_backend.speech.domain.job_dispatch import (
    ClaimedJob,
    DispatchBatch,
    DispatchClaimLostError,
    DispatchConflictError,
    DispatchJob,
    DispatchLimits,
    ExecutionSubmission,
    GatewayReply,
    PublishedResult,
    ResultReceipt,
)

NOW = 1_700_000_000_000_000
DIGEST = "sha256:" + "a" * 64
SECRET = "synthetic-private-component-token"


def _job(*, state: str = "pending", version: int = 1) -> DispatchJob:
    document = json.dumps({"kind": "tts", "input": {"text": "synthetic"}})
    return DispatchJob(
        "job-synthetic-0001",
        "workspace-default",
        "tts",
        state,  # type: ignore[arg-type]
        version,
        NOW,
        "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest(),
        document,
    )


def _settings(**changes: object) -> JobDispatchSettings:
    values: dict[str, object] = {
        "process_role": "job-worker",
        "node_id": "node-local-1",
        "instance_id": "dispatch-1",
        "database_url": SecretStr(
            "postgresql://dispatch:synthetic-private-password@127.0.0.1:55432/test"
        ),
        "database_schema": "speech_test",
        "gateway_base_url": "http://127.0.0.1:8010",
        "gateway_credential": SecretStr(SECRET),
        "gateway_timeout_seconds": 1.0,
        "batch_size": 8,
        "lease_ms": 30_000,
        "poll_seconds": 0.01,
        "job_timeout_seconds": 600.0,
        "max_attempts": 5,
        "interval_seconds": 1.0,
        "storage_timeout_seconds": 3.0,
    }
    return JobDispatchSettings.model_validate(values | changes)


def test_configuration_requires_every_operating_value_before_io() -> None:
    with pytest.raises(ValidationError):
        JobDispatchSettings.model_validate(
            {
                key: value
                for key, value in _settings().model_dump().items()
                if key != "gateway_base_url"
            }
        )
    with pytest.raises(ValidationError):
        _settings(gateway_credential=SecretStr("short"))
    with pytest.raises(ValidationError):
        _settings(batch_size=None)
    with pytest.raises(ValidationError):
        _settings(lease_ms=600_001)
    with pytest.raises(ValidationError):
        _settings(process_role="api")


def test_configuration_derives_explicit_worker_identity_without_secret_repr() -> None:
    settings = _settings()
    assert settings.worker_id == "node-local-1:dispatch-1"
    text = repr(settings)
    assert SECRET not in text and "synthetic-private-password" not in text
    assert "127.0.0.1" not in text


def test_configuration_rejects_plain_http_gateway_outside_loopback() -> None:
    with pytest.raises(ValidationError):
        _settings(gateway_base_url="http://gateway.internal:8010")


def test_production_dependencies_fail_closed() -> None:
    with pytest.raises(DispatchDependenciesUnavailableError):
        build_dispatch_dependencies(_settings())


@pytest.mark.parametrize(
    "missing",
    (
        "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
        "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
    ),
)
def test_cli_refuses_missing_gateway_configuration_before_io(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    settings = _settings()
    environment = {
        "SPEECHNEXUS_PROCESS_ROLE": settings.process_role,
        "SPEECHNEXUS_NODE_ID": settings.node_id,
        "SPEECHNEXUS_INSTANCE_ID": settings.instance_id,
        "SPEECHNEXUS_DATABASE_URL": settings.database_url.get_secret_value(),
        "SPEECHNEXUS_DATABASE_SCHEMA": settings.database_schema,
        "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": str(settings.gateway_base_url),
        "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": (
            settings.gateway_credential.get_secret_value()
        ),
        "SPEECHNEXUS_EXECUTION_GATEWAY_TIMEOUT_SECONDS": str(settings.gateway_timeout_seconds),
        "SPEECHNEXUS_JOB_DISPATCH_BATCH_SIZE": str(settings.batch_size),
        "SPEECHNEXUS_JOB_DISPATCH_LEASE_MS": str(settings.lease_ms),
        "SPEECHNEXUS_JOB_DISPATCH_POLL_SECONDS": str(settings.poll_seconds),
        "SPEECHNEXUS_JOB_DISPATCH_JOB_TIMEOUT_SECONDS": str(settings.job_timeout_seconds),
        "SPEECHNEXUS_JOB_DISPATCH_MAX_ATTEMPTS": str(settings.max_attempts),
        "SPEECHNEXUS_JOB_DISPATCH_INTERVAL_SECONDS": str(settings.interval_seconds),
        "SPEECHNEXUS_JOB_DISPATCH_STORAGE_TIMEOUT_SECONDS": str(settings.storage_timeout_seconds),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing)
    monkeypatch.setattr(sys, "argv", ["speechnexus-job-dispatch-worker", "--once"])

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 1


def test_batch_log_has_fixed_counter_fields_only() -> None:
    document = batch_log(DispatchBatch(claimed=3, succeeded=1, failed=1, lease_reclaimed=1))
    assert tuple(document) == BATCH_LOG_FIELDS
    assert document == {
        "event": "job_dispatch_batch",
        "claimed": 3,
        "succeeded": 1,
        "failed": 1,
        "unknown": 0,
        "skipped": 0,
        "deferred": 0,
        "lease_reclaimed": 1,
        "lease_lost": 0,
    }


class LeaseWorld:
    """One Job whose coordination lease and public state share a fake clock."""

    def __init__(self, job: DispatchJob) -> None:
        self.job = job
        self.clock = [NOW]
        self.owner: str | None = None
        self.claim_version = 0
        self.expires_at_us = 0
        self.finish_calls: list[tuple[str, str]] = []

    def now(self) -> int:
        return self.clock[0]

    async def claim_due(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]:
        if self.job.state in ("succeeded", "failed"):
            return []
        if self.owner is not None and self.expires_at_us > self.clock[0]:
            return []
        self.claim_version += 1
        self.owner = worker_id
        self.expires_at_us = self.clock[0] + lease_ms * 1000
        return [ClaimedJob(self.job, worker_id, self.claim_version, self.expires_at_us)]

    async def reload(self, job_id: str) -> DispatchJob:
        return self.job

    async def mark_running(self, job: DispatchJob) -> DispatchJob:
        if self.job.state == "running":
            return self.job
        if self.job.state != "pending" or self.job.status_version != job.status_version:
            raise DispatchConflictError("running guard lost")
        self.job = replace(self.job, state="running", status_version=self.job.status_version + 1)
        return self.job

    async def renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob | None:
        if self.owner != claim.worker_id or self.claim_version != claim.claim_version:
            return None
        self.expires_at_us = self.clock[0] + lease_ms * 1000
        return replace(claim, lease_expires_at_us=self.expires_at_us)

    async def finish(
        self,
        claim: ClaimedJob,
        target: str,
        *,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        self.finish_calls.append((claim.worker_id, target))
        if (
            self.owner != claim.worker_id
            or self.claim_version != claim.claim_version
            or self.expires_at_us <= self.clock[0]
        ):
            raise DispatchClaimLostError("lease lost")
        if self.job.state in ("succeeded", "failed"):
            return self.job
        if self.job.status_version != claim.job.status_version or self.job.state != claim.job.state:
            raise DispatchClaimLostError("terminal guard lost")
        self.job = replace(
            self.job,
            state=cast(Any, target),
            status_version=self.job.status_version + 1,
        )
        self.owner = None
        self.expires_at_us = 0
        return self.job


class Planner:
    async def plan(self, job: DispatchJob, submit_intent_id: str) -> ExecutionSubmission:
        return ExecutionSubmission(
            submit_intent_id,
            {"submit_intent_id": submit_intent_id, "command_digest": DIGEST},
        )


class Publisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def publish(
        self, job: DispatchJob, submission: ExecutionSubmission, receipt: Mapping[str, Any]
    ) -> PublishedResult:
        self.calls.append(job.job_id)
        return PublishedResult("artifact-synthetic", "receipt-synthetic")


class Gateway:
    def __init__(self, states: list[str]) -> None:
        self.states = states
        self.queries = 0

    async def query(self, submit_intent_id: str) -> GatewayReply:
        state = self.states[min(self.queries, len(self.states) - 1)]
        self.queries += 1
        return GatewayReply(
            "status",
            {"submit_intent_id": submit_intent_id, "execution_state": state},
        )

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply:
        raise AssertionError("the fake query already exists")

    async def result_receipt(self, submit_intent_id: str, *, audio: bool) -> ResultReceipt:
        return ResultReceipt("status", {"reference": {"asset_or_artifact_id": "artifact-1"}})


class Gate:
    def __init__(self) -> None:
        self.sleeping = asyncio.Event()
        self.resume = asyncio.Event()

    async def sleep(self, _: float) -> None:
        self.sleeping.set()
        await self.resume.wait()


def _dispatcher(
    world: LeaseWorld, gateway: Gateway, publisher: Publisher, *, sleep: Any = None
) -> JobDispatcher:
    return JobDispatcher(
        world,
        world,
        Planner(),
        gateway,
        publisher,
        limits=DispatchLimits(poll_seconds=0.01, job_timeout_seconds=600, lease_ms=30_000),
        now_us=world.now,
        sleep=sleep or (lambda _: asyncio.sleep(0)),
    )


async def test_stale_worker_never_finishes_or_publishes_after_takeover() -> None:
    world = LeaseWorld(_job())
    gateway = Gateway(["executing", "succeeded", "succeeded"])
    publisher = Publisher()
    gate = Gate()
    first = _dispatcher(world, gateway, publisher, sleep=gate.sleep)
    second = _dispatcher(world, gateway, publisher)
    batches: list[DispatchBatch] = []

    task = asyncio.create_task(
        run_dispatch_worker(
            first,
            "worker-a",
            asyncio.Event(),
            interval_seconds=1,
            once=True,
            on_batch=batches.append,
        )
    )
    await asyncio.wait_for(gate.sleeping.wait(), timeout=1)
    world.clock[0] += 31_000_000
    batch_b = await second.run_once("worker-b")
    gate.resume.set()
    await task

    assert batch_b.claimed == 1 and batch_b.succeeded == 1
    assert batch_b.lease_reclaimed == 1
    assert len(batches) == 1
    assert batches[0].lease_lost == 1 and batches[0].succeeded == 0
    assert publisher.calls == [_job().job_id]
    assert world.finish_calls == [("worker-b", "succeeded")]
    assert world.job.state == "succeeded"


async def test_graceful_stop_defers_then_lease_expiry_lets_another_worker_finish() -> None:
    world = LeaseWorld(_job())
    gateway = Gateway(["executing", "succeeded", "succeeded"])
    publisher = Publisher()
    gate = Gate()
    first = _dispatcher(world, gateway, publisher, sleep=gate.sleep)
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_dispatch_worker(first, "worker-a", stop, interval_seconds=1, once=True)
    )
    await asyncio.wait_for(gate.sleeping.wait(), timeout=1)
    stop.set()
    gate.resume.set()
    await task

    assert world.job.state == "running"
    assert world.finish_calls == []
    assert publisher.calls == []
    assert world.owner == "worker-a"

    world.clock[0] += 31_000_000
    second = _dispatcher(world, gateway, publisher)
    batch_b = await second.run_once("worker-b")

    assert batch_b.lease_reclaimed == 1 and batch_b.succeeded == 1
    assert publisher.calls == [_job().job_id]
    assert world.finish_calls == [("worker-b", "succeeded")]


async def test_two_entry_workers_claim_and_publish_once() -> None:
    world = LeaseWorld(_job())
    gateway = Gateway(["succeeded"])
    publisher = Publisher()
    first = _dispatcher(world, gateway, publisher)
    second = _dispatcher(world, gateway, publisher)
    batches: list[DispatchBatch] = []

    def collect(batch: DispatchBatch) -> None:
        batches.append(batch)

    await asyncio.gather(
        run_dispatch_worker(
            first,
            "worker-a",
            asyncio.Event(),
            interval_seconds=1,
            once=True,
            on_batch=collect,
        ),
        run_dispatch_worker(
            second,
            "worker-b",
            asyncio.Event(),
            interval_seconds=1,
            once=True,
            on_batch=collect,
        ),
    )

    assert len(batches) == 2
    assert sum(batch.claimed for batch in batches) == 1
    assert sum(batch.succeeded for batch in batches) == 1
    assert publisher.calls == [_job().job_id]
    assert world.job.state == "succeeded"
    assert len(world.finish_calls) == 1


async def test_entry_loop_logs_one_stable_batch_line() -> None:
    world = LeaseWorld(_job())
    batch = await _dispatcher(world, Gateway(["succeeded"]), Publisher()).run_once("worker-a")
    assert tuple(batch_log(batch)) == BATCH_LOG_FIELDS
    assert json.loads(json.dumps(batch_log(batch)))["succeeded"] == 1
