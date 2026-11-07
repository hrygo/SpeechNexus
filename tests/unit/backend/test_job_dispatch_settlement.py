"""Expiry settlement, absent-execution proof and capacity return."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher
from speechnexus_backend.speech.domain.job_dispatch import (
    ClaimedJob,
    DispatchJob,
    DispatchLimits,
    DispatchProtocolError,
    DispatchRequestUnavailableError,
    ExecutionSubmission,
    submit_intent_identity,
)
from tests.unit.backend.test_job_dispatch import (
    DIGEST,
    NOW,
    GatewayState,
    Publisher,
    Queue,
    States,
    _gateway,
    _job,
)

PAST = "2020-01-01T00:00:00.000000Z"
FUTURE = "2030-01-01T00:00:00.000000Z"


class _HandlerSource(Protocol):
    def handler(self, request: httpx.Request) -> httpx.Response: ...


def _submission(job: DispatchJob, submit_id: str, *, deadline_at: str) -> ExecutionSubmission:
    return ExecutionSubmission(
        submit_id,
        {
            "submit_intent_id": submit_id,
            "lease_id": "lease-synthetic",
            "operation_id": job.job_id,
            "attempt_id": "attempt-synthetic",
            "stage_id": "stage-synthetic",
            "workspace_id": job.workspace_id,
            "pool_id": "batch",
            "host_node_id": "node-synthetic",
            "gateway_id": "gateway-synthetic",
            "instance_id": "instance-synthetic",
            "instance_generation": 1,
            "binding_revision_id": "binding-rev-1",
            "policy_revision_id": "policy-rev-1",
            "voice_revision_id": None,
            "fencing_token": 1,
            "command_digest": DIGEST,
            "command": {"schema_version": "1.0.0", "kind": job.kind},
            "deadline_at": deadline_at,
            "max_output_audio_ms": 1000,
            "sample_rate_hz": 24000,
        },
    )


class DeadlinePlanner:
    def __init__(self, deadline_at: str) -> None:
        self.deadline_at = deadline_at

    async def plan(self, job: DispatchJob, submit_id: str) -> ExecutionSubmission:
        return _submission(job, submit_id, deadline_at=self.deadline_at)


class ProtocolBrokenPlanner:
    async def plan(self, job: DispatchJob, submit_id: str) -> ExecutionSubmission:
        raise DispatchProtocolError("stored command disagrees")


class UnavailablePlanner:
    async def plan(self, job: DispatchJob, submit_id: str) -> ExecutionSubmission:
        raise DispatchRequestUnavailableError


class Settler:
    def __init__(self, result: bool = True) -> None:
        self.calls: list[str] = []
        self.result = result

    async def release(self, operation_id: str) -> bool:
        self.calls.append(operation_id)
        return self.result


class ExplodingSettler(Settler):
    async def release(self, operation_id: str) -> bool:
        self.calls.append(operation_id)
        raise RuntimeError("lease storage is down")


class _Source:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler

    def handler(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


def _not_found_gateway() -> GatewayState:
    """A gateway that has never accepted a submit: every query answers 404."""
    return GatewayState()


def _executing_source(job: DispatchJob) -> _Source:
    identity = submit_intent_identity(job.job_id)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith(identity)
        return httpx.Response(
            200, json={"submit_intent_id": identity, "execution_state": "executing"}
        )

    return _Source(handler)


def _unavailable_source() -> _Source:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            503, json={"error": {"code": "control_outcome_unknown", "message": "unknown"}}
        )

    return _Source(handler)


def _asr_job() -> DispatchJob:
    document = json.dumps({"kind": "asr_file"})
    digest = "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()
    return DispatchJob(
        "job-synthetic-asr", "workspace-default", "asr", "pending", 1, NOW, digest, document
    )


def _dispatcher(
    job: DispatchJob,
    gateway: _HandlerSource,
    *,
    planner: Any,
    settler: Settler | None = None,
) -> tuple[JobDispatcher, States]:
    states = States(job)
    dispatcher = JobDispatcher(
        Queue([[ClaimedJob(job, "worker-a", 1, NOW + 60_000_000)]]),
        states,
        planner,
        _gateway(gateway.handler),
        Publisher(),
        limits=DispatchLimits(poll_seconds=0.01, job_timeout_seconds=10),
        now_us=lambda: NOW,
        sleep=lambda _: asyncio.sleep(0),
    )
    if settler is not None:
        dispatcher.settle_capacity_through(settler)
    return dispatcher, states


async def test_expired_frozen_deadline_with_gateway_not_found_settles_and_releases() -> None:
    job = _job()
    settler = Settler()
    gateway = _not_found_gateway()
    dispatcher, states = _dispatcher(job, gateway, planner=DeadlinePlanner(PAST), settler=settler)
    batch = await dispatcher.run_once("worker-a")

    assert batch.claimed == 1 and batch.failed == 1
    assert gateway.submit_calls == 0
    assert states.jobs[job.job_id].state == "failed"
    assert states.events == [(job.job_id, 1, "running"), (job.job_id, 2, "failed")]
    assert settler.calls == [job.job_id]


async def test_deadline_alone_never_settles_without_a_gateway_not_found() -> None:
    job = _job()
    settler = Settler()
    dispatcher, states = _dispatcher(
        job, _unavailable_source(), planner=DeadlinePlanner(PAST), settler=settler
    )
    batch = await dispatcher.run_once("worker-a")

    assert batch.claimed == 1 and batch.unknown == 1
    assert states.jobs[job.job_id].state == "running"
    assert states.events == [(job.job_id, 1, "running")]
    assert settler.calls == []


async def test_unreadable_stored_command_never_fails_a_possible_execution() -> None:
    job = _job(state="running", version=2)
    settler = Settler()
    dispatcher, states = _dispatcher(
        job, _executing_source(job), planner=ProtocolBrokenPlanner(), settler=settler
    )
    batch = await dispatcher.run_once("worker-a")

    assert batch.claimed == 1 and batch.unknown == 1
    assert states.jobs[job.job_id].state == "running"
    assert states.events == []
    assert states.not_submitted == []
    assert settler.calls == []


async def test_unreadable_stored_command_with_gateway_not_found_fails_the_job() -> None:
    job = _job(state="running", version=2)
    settler = Settler()
    dispatcher, states = _dispatcher(
        job, _not_found_gateway(), planner=ProtocolBrokenPlanner(), settler=settler
    )
    batch = await dispatcher.run_once("worker-a")

    assert batch.claimed == 1 and batch.failed == 1
    assert states.jobs[job.job_id].state == "failed"
    assert settler.calls == [job.job_id]


async def test_missing_stored_document_with_gateway_not_found_fails_the_job() -> None:
    job = _job(state="running", version=2)
    dispatcher, states = _dispatcher(job, _not_found_gateway(), planner=UnavailablePlanner())
    batch = await dispatcher.run_once("worker-a")

    assert batch.claimed == 1 and batch.failed == 1
    assert states.jobs[job.job_id].state == "failed"


async def test_asr_terminal_success_returns_capacity_through_the_lease_service() -> None:
    job = _asr_job()
    settler = Settler()
    dispatcher, states = _dispatcher(
        job, _not_found_gateway(), planner=DeadlinePlanner(FUTURE), settler=settler
    )
    batch = await dispatcher.run_once("worker-a")

    assert batch.claimed == 1 and batch.succeeded == 1
    assert states.jobs[job.job_id].state == "succeeded"
    assert settler.calls == [job.job_id]


async def test_release_failure_never_rewrites_the_committed_terminal_outcome() -> None:
    job = _job()
    settler = ExplodingSettler()
    dispatcher, states = _dispatcher(
        job, _not_found_gateway(), planner=DeadlinePlanner(FUTURE), settler=settler
    )
    batch = await dispatcher.run_once("worker-a")

    assert batch.claimed == 1 and batch.succeeded == 1
    assert states.jobs[job.job_id].state == "succeeded"
    assert settler.calls == [job.job_id]
