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
    DispatchRejectedError,
    DispatchRequestUnavailableError,
    DispatchUnavailableError,
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
        "object_storage_endpoint": "http://127.0.0.1:59000",
        "artifacts_bucket": "synthetic-artifacts",
        "execution_result_bucket": "synthetic-results",
        "object_storage_access_key": SecretStr("synthetic-access-key"),
        "object_storage_secret_key": SecretStr("synthetic-secret-key"),
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
    with pytest.raises(ValidationError):
        _settings(artifacts_bucket="INVALID")
    with pytest.raises(ValidationError):
        _settings(object_storage_endpoint="http://127.0.0.1:59000/private")


def test_configuration_derives_explicit_worker_identity_without_secret_repr() -> None:
    settings = _settings()
    assert settings.worker_id == "node-local-1:dispatch-1"
    text = repr(settings)
    assert SECRET not in text and "synthetic-private-password" not in text
    assert "127.0.0.1" not in text


def test_configuration_rejects_plain_http_gateway_outside_loopback() -> None:
    with pytest.raises(ValidationError):
        _settings(gateway_base_url="http://gateway.internal:8010")


def test_dispatch_lane_defaults_to_the_asr_and_tts_contract_kinds() -> None:
    assert _settings().kinds == ("asr", "tts")
    assert _settings(dispatch_kinds=" asr , tts ".replace(" ", "")).kinds == ("asr", "tts")
    assert _settings(dispatch_kinds="tts").kinds == ("tts",)
    for invalid in ("", "asr,asr", "asr,voice_unknown"):
        with pytest.raises(ValidationError):
            _settings(dispatch_kinds=invalid)


def test_production_dependencies_fail_closed() -> None:
    with pytest.raises(DispatchDependenciesUnavailableError):
        build_dispatch_dependencies(_settings())


def test_production_dependencies_assemble_the_real_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import speechnexus_backend.job_dispatch_runtime as runtime

    class Pool:
        opened = False
        closed = False

        def open(self) -> None:
            self.opened = True

        def close(self) -> None:
            self.closed = True

    pool, publisher = Pool(), object()
    monkeypatch.setattr(runtime, "speech_pool", lambda *a, **k: pool)
    monkeypatch.setattr(runtime, "build_result_publisher", lambda *a, **k: publisher)

    dependencies = build_dispatch_dependencies(_settings())

    assert pool.opened and not pool.closed
    assert cast(Any, dependencies.pool) is pool
    assert dependencies.results is publisher
    assert type(dependencies.planner).__name__ == "StoredCommandPlanner"


def test_production_dependencies_close_the_pool_when_assembly_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import speechnexus_backend.job_dispatch_runtime as runtime

    class Pool:
        closed = False

        def open(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    pool = Pool()

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("object storage unreachable")

    monkeypatch.setattr(runtime, "speech_pool", lambda *a, **k: pool)
    monkeypatch.setattr(runtime, "build_result_publisher", broken)

    with pytest.raises(DispatchDependenciesUnavailableError):
        build_dispatch_dependencies(_settings())
    assert pool.closed


def _environment(settings: JobDispatchSettings) -> dict[str, str]:
    return {
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
        "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": str(settings.object_storage_endpoint),
        "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": settings.artifacts_bucket,
        "SPEECHNEXUS_EXECUTION_RESULT_BUCKET": settings.execution_result_bucket,
        "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": (
            settings.object_storage_access_key.get_secret_value()
        ),
        "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": (
            settings.object_storage_secret_key.get_secret_value()
        ),
    }


@pytest.mark.parametrize(
    "missing",
    (
        "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
        "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
        "SPEECHNEXUS_OBJECT_STORAGE_BUCKET",
        "SPEECHNEXUS_EXECUTION_RESULT_BUCKET",
        "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY",
        "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY",
    ),
)
def test_cli_refuses_missing_gateway_configuration_before_io(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    settings = _settings()
    for key, value in _environment(settings).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing)
    monkeypatch.setattr(sys, "argv", ["speechnexus-job-dispatch-worker", "--once"])

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 1


def test_cli_exits_2_before_building_the_runtime_when_assembly_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import speechnexus_backend.job_dispatch_worker as worker

    settings = _settings()
    for key, value in _environment(settings).items():
        monkeypatch.setenv(key, value)
    # A closed port makes the object-storage readiness check fail during assembly.
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setattr(sys, "argv", ["speechnexus-job-dispatch-worker", "--once"])
    built: list[object] = []
    monkeypatch.setattr(worker, "JobDispatchRuntime", lambda *a, **k: built.append(object()))

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 2
    assert built == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no lease was claimed" in captured.err


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
        self.not_submitted: list[tuple[str, str]] = []
        self.progress: list[tuple[str, str, int, int | None]] = []

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

    async def record_cancellation_not_submitted(self, job_id: str, *, observer: str) -> None:
        self.not_submitted.append((job_id, observer))

    async def record_progress(
        self, claim: ClaimedJob, *, unit: str, completed: int, total: int | None
    ) -> bool:
        if any(item[2] >= completed for item in self.progress):
            return False
        self.progress.append((claim.job.job_id, unit, completed, total))
        return True

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
    world: LeaseWorld,
    gateway: Gateway,
    publisher: Publisher,
    *,
    sleep: Any = None,
    planner: Any = None,
) -> JobDispatcher:
    return JobDispatcher(
        world,
        world,
        planner or Planner(),
        gateway,
        publisher,
        limits=DispatchLimits(poll_seconds=0.01, job_timeout_seconds=600, lease_ms=30_000),
        now_us=world.now,
        sleep=sleep or (lambda _: asyncio.sleep(0)),
    )


class CancelledDuringDispatch(LeaseWorld):
    """A cancellation lands after mark_running but before the gateway submit."""

    async def mark_running(self, job: DispatchJob) -> DispatchJob:
        running = await super().mark_running(job)
        self.job = replace(running, state="cancelling", status_version=running.status_version + 1)
        return running


async def test_accepted_cancellation_before_submit_never_starts_an_execution() -> None:
    world = CancelledDuringDispatch(_job())
    gateway = Gateway(["executing"])
    publisher = Publisher()
    batch = await _dispatcher(world, gateway, publisher).run_once("worker-a")

    assert batch.claimed == 1 and batch.lease_lost == 1
    assert gateway.queries == 0
    assert publisher.calls == []
    assert world.job.state == "cancelling"
    assert world.finish_calls == []
    assert world.not_submitted == [(world.job.job_id, "worker-a")]


class CancellationAtGatewaySubmit(Gateway):
    """Acceptance commits at the gateway submit after the local reload guard."""

    def __init__(self, world: LeaseWorld) -> None:
        super().__init__([])
        self.world = world

    async def query(self, submit_intent_id: str) -> GatewayReply:
        return GatewayReply("not_found")

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply:
        self.world.job = replace(
            self.world.job,
            state="cancelling",
            status_version=self.world.job.status_version + 1,
        )
        raise DispatchRejectedError("gateway_barrier_fenced")


class StopAfterPlan(LeaseWorld):
    """A graceful stop observed after mark_running/plan but before submit."""

    calls = 0

    def stop(self) -> bool:
        self.calls += 1
        if self.calls == 1:
            return False
        self.job = replace(self.job, state="cancelling", status_version=self.job.status_version + 1)
        return True


async def test_graceful_stop_after_plan_records_not_submitted_observation() -> None:
    world = StopAfterPlan(_job())
    gateway = Gateway(["executing"])
    batch = await _dispatcher(world, gateway, Publisher()).run_once(
        "worker-a", should_stop=world.stop
    )

    assert batch.claimed == 1 and batch.deferred == 1
    assert gateway.queries == 0
    assert world.job.state == "cancelling"
    assert world.finish_calls == []
    assert world.not_submitted == [(world.job.job_id, "worker-a")]


class CancelledAtFinish(LeaseWorld):
    """The cancellation lands after the gateway answer, before the terminal write."""

    async def finish(
        self,
        claim: ClaimedJob,
        target: str,
        *,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        if self.job.state == "running":
            self.job = replace(
                self.job, state="cancelling", status_version=self.job.status_version + 1
            )
        return await super().finish(claim, target, result_json=result_json, error_code=error_code)


class UnavailablePlanner(Planner):
    async def plan(self, job: DispatchJob, submit_intent_id: str) -> ExecutionSubmission:
        raise DispatchRequestUnavailableError


class AbsentGateway(Gateway):
    """The gateway confirms no execution exists for the stable identity."""

    async def query(self, submit_intent_id: str) -> GatewayReply:
        self.queries += 1
        return GatewayReply("not_found")


async def test_pre_submit_failure_with_a_possible_execution_stays_unknown() -> None:
    # A previous claimant may have submitted before this worker read the stored
    # command. An executing answer forbids a terminal failure and the durable
    # "not submitted" observation.
    world = CancelledAtFinish(_job(state="running", version=2))
    gateway = Gateway(["executing"])
    batch = await _dispatcher(world, gateway, Publisher(), planner=UnavailablePlanner()).run_once(
        "worker-a"
    )

    assert batch.claimed == 1 and batch.unknown == 1
    assert gateway.queries == 1
    assert world.finish_calls == []
    assert world.not_submitted == []


async def test_pre_submit_failure_with_confirmed_absence_records_not_submitted() -> None:
    world = CancelledAtFinish(_job(state="running", version=2))
    gateway = AbsentGateway([])
    batch = await _dispatcher(world, gateway, Publisher(), planner=UnavailablePlanner()).run_once(
        "worker-a"
    )

    assert batch.claimed == 1 and batch.lease_lost == 1
    assert gateway.queries == 1
    assert world.job.state == "cancelling"
    assert world.not_submitted == [(world.job.job_id, "worker-a")]


async def test_a_pending_claim_that_fails_before_submit_queries_nothing() -> None:
    # This worker marked the Job running itself and failed before any send; a
    # cancellation cannot hide an execution, so the gateway is never asked.
    world = CancelledDuringDispatch(_job())
    gateway = Gateway(["executing"])
    batch = await _dispatcher(world, gateway, Publisher(), planner=UnavailablePlanner()).run_once(
        "worker-a"
    )

    assert batch.claimed == 1 and batch.lease_lost == 1
    assert gateway.queries == 0
    assert world.job.state == "cancelling"
    assert world.not_submitted == [(world.job.job_id, "worker-a")]


async def test_gateway_barrier_fenced_keeps_cancelling_without_failure() -> None:
    world = LeaseWorld(_job())
    batch = await _dispatcher(world, CancellationAtGatewaySubmit(world), Publisher()).run_once(
        "worker-a"
    )

    assert batch.claimed == 1 and batch.skipped == 1 and batch.failed == 0
    assert world.job.state == "cancelling"
    assert world.finish_calls == []
    # The barrier proves no execution; that fact is what reconciliation settles.
    assert world.not_submitted == [(world.job.job_id, "worker-a")]


class ExecutionObservedThenCancelled(LeaseWorld):
    """An execution is already visible when the claim is taken away."""

    async def renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob | None:
        if self.job.state == "cancelling":
            return None
        return await super().renew(claim, lease_ms)


class ExecutingThenCancelled(Gateway):
    def __init__(self, world: ExecutionObservedThenCancelled) -> None:
        super().__init__([])
        self.world = world

    async def query(self, submit_intent_id: str) -> GatewayReply:
        self.queries += 1
        self.world.job = replace(
            self.world.job,
            state="cancelling",
            status_version=self.world.job.status_version + 1,
        )
        return GatewayReply(
            "status", {"submit_intent_id": submit_intent_id, "execution_state": "executing"}
        )


async def test_observed_execution_is_never_recorded_as_not_submitted() -> None:
    # The gateway already holds an execution for this identity. Losing the claim
    # (with a cancellation accepted meanwhile) must not forge a "not submitted"
    # observation, or reconciliation could release capacity of a running job.
    world = ExecutionObservedThenCancelled(_job(state="running", version=2))
    batch = await _dispatcher(world, ExecutingThenCancelled(world), Publisher()).run_once(
        "worker-a"
    )

    assert batch.claimed == 1 and batch.lease_lost == 1
    assert world.job.state == "cancelling"
    assert world.finish_calls == []
    assert world.not_submitted == []


class UnknownSubmitAfterCancel(Gateway):
    """The submit answer is lost and a cancellation lands meanwhile."""

    def __init__(self, world: LeaseWorld) -> None:
        super().__init__([])
        self.world = world

    async def query(self, submit_intent_id: str) -> GatewayReply:
        return GatewayReply("not_found")

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply:
        self.world.job = replace(
            self.world.job,
            state="cancelling",
            status_version=self.world.job.status_version + 1,
        )
        raise DispatchUnavailableError("synthetic lost submit response")


class CancelledBeforeQueryGuard(LeaseWorld):
    """A running Job's cancellation lands between renew and the reload guard."""

    async def reload(self, job_id: str) -> DispatchJob:
        self.job = replace(
            self.job,
            state="cancelling",
            status_version=self.job.status_version + 1,
        )
        return self.job


async def test_cancelled_running_job_before_any_query_records_no_absence() -> None:
    # The worker never asked the gateway and never sent anything, but the Job was
    # already marked running by an earlier claim: no absence can be proven here.
    world = CancelledBeforeQueryGuard(_job(state="running", version=2))
    gateway = Gateway(["executing"])
    batch = await _dispatcher(world, gateway, Publisher()).run_once("worker-a")

    assert batch.claimed == 1 and batch.lease_lost == 1
    assert gateway.queries == 0
    assert world.job.state == "cancelling"
    assert world.not_submitted == []


class UnavailableQueryThenCancelled(Gateway):
    def __init__(self, world: LeaseWorld) -> None:
        super().__init__([])
        self.world = world

    async def query(self, submit_intent_id: str) -> GatewayReply:
        self.queries += 1
        self.world.job = replace(
            self.world.job,
            state="cancelling",
            status_version=self.world.job.status_version + 1,
        )
        raise DispatchUnavailableError("synthetic query outage")


async def test_unreadable_query_after_cancellation_records_no_absence() -> None:
    world = LeaseWorld(_job())
    batch = await _dispatcher(world, UnavailableQueryThenCancelled(world), Publisher()).run_once(
        "worker-a"
    )

    assert batch.claimed == 1 and batch.unknown == 1
    assert world.job.state == "cancelling"
    assert world.not_submitted == []


async def test_lost_submit_response_is_never_recorded_as_not_submitted() -> None:
    world = LeaseWorld(_job())
    batch = await _dispatcher(world, UnknownSubmitAfterCancel(world), Publisher()).run_once(
        "worker-a"
    )

    assert batch.claimed == 1 and batch.unknown == 1
    assert world.job.state == "cancelling"
    assert world.not_submitted == []


class BarrierFencedAtGatewaySubmit(Gateway):
    """A barrier refusal that is not preceded by an accepted cancellation."""

    def __init__(self) -> None:
        super().__init__([])

    async def query(self, submit_intent_id: str) -> GatewayReply:
        return GatewayReply("not_found")

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply:
        raise DispatchRejectedError("gateway_barrier_fenced")


async def test_gateway_barrier_fenced_without_cancelling_still_fails_job() -> None:
    world = LeaseWorld(_job())
    batch = await _dispatcher(world, BarrierFencedAtGatewaySubmit(), Publisher()).run_once(
        "worker-a"
    )

    assert batch.claimed == 1 and batch.failed == 1 and batch.skipped == 0
    assert world.job.state == "failed"
    assert world.finish_calls == [("worker-a", "failed")]


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


class CancelledAtClaim(LeaseWorld):
    """Cancellation commits after claim_due but before the batch dispatches it."""

    async def claim_due(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]:
        claims = await super().claim_due(worker_id, lease_ms, limit)
        self.job = replace(self.job, state="cancelling", status_version=self.job.status_version + 1)
        return claims


async def test_batch_stop_records_not_submitted_for_deferred_cancelling_claim() -> None:
    world = CancelledAtClaim(_job())
    batch = await _dispatcher(world, Gateway(["executing"]), Publisher()).run_once(
        "worker-a", should_stop=lambda: True
    )

    assert batch.claimed == 1 and batch.deferred == 1
    assert world.job.state == "cancelling"
    assert world.not_submitted == [(world.job.job_id, "worker-a")]


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
