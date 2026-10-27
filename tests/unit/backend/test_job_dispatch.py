"""Dispatcher semantics against an HTTP fake; no real engine or object store."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import httpx
import pytest

from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher
from speechnexus_backend.speech.domain.job_dispatch import (
    ClaimedJob,
    DispatchClaimLostError,
    DispatchConflictError,
    DispatchJob,
    DispatchLimits,
    ExecutionSubmission,
    PublishedResult,
    submit_intent_identity,
)
from speechnexus_backend.speech.infrastructure.execution_gateway_http import (
    ExecutionGatewayEndpoint,
    HttpExecutionGateway,
)

NOW = 1_700_000_000_000_000
DIGEST = "sha256:" + "a" * 64


def _job(*, state: str = "pending", version: int = 1) -> DispatchJob:
    document = json.dumps({"kind": "tts", "input": {"text": "synthetic"}})
    digest = "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()
    return DispatchJob(
        "job-synthetic-0001",
        "workspace-default",
        "tts",
        state,  # type: ignore[arg-type]
        version,
        NOW,
        digest,
        document,
    )


class Queue:
    def __init__(self, batches: list[list[ClaimedJob]]) -> None:
        self.batches = batches

    async def claim_due(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]:
        return self.batches.pop(0) if self.batches else []


class States:
    def __init__(self, job: DispatchJob) -> None:
        self.jobs = {job.job_id: job}
        self.events: list[tuple[str, int, str]] = []

    async def reload(self, job_id: str) -> DispatchJob:
        return self.jobs[job_id]

    async def mark_running(self, job: DispatchJob) -> DispatchJob:
        current = self.jobs[job.job_id]
        if current.state in ("succeeded", "failed") or current.state == "running":
            return current
        if current.state != "pending" or current.status_version != job.status_version:
            raise DispatchConflictError("running guard lost")
        updated = replace(current, state="running", status_version=current.status_version + 1)
        self.jobs[job.job_id] = updated
        self.events.append((updated.job_id, updated.status_version - 1, "running"))
        return updated

    async def renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob:
        return replace(claim, lease_expires_at_us=claim.lease_expires_at_us + lease_ms * 1000)

    async def finish(
        self,
        claim: ClaimedJob,
        target: str,
        *,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        job = claim.job
        current = self.jobs[job.job_id]
        if current.state in ("succeeded", "failed"):
            return current
        if current.status_version != job.status_version or current.state != job.state:
            raise DispatchClaimLostError("terminal guard lost")
        if (target == "succeeded") != (result_json is not None):
            raise AssertionError("bad result shape")
        if (target == "failed") != (error_code is not None):
            raise AssertionError("bad error shape")
        updated = replace(
            current,
            state=target,  # type: ignore[arg-type]
            status_version=current.status_version + 1,
        )
        self.jobs[job.job_id] = updated
        self.events.append((updated.job_id, updated.status_version - 1, target))
        return updated


class Planner:
    async def plan(self, job: DispatchJob, submit_id: str) -> ExecutionSubmission:
        return ExecutionSubmission(
            submit_id,
            {
                "submit_intent_id": submit_id,
                "lease_id": "lease-synthetic",
                "operation_id": job.job_id,
                "attempt_id": "attempt-synthetic",
                "stage_id": "stage-synthetic",
                "workspace_id": job.workspace_id,
                "pool_id": "tts-batch",
                "host_node_id": "node-synthetic",
                "gateway_id": "gateway-synthetic",
                "instance_id": "instance-synthetic",
                "instance_generation": 1,
                "binding_revision_id": "binding-rev-1",
                "policy_revision_id": "policy-rev-1",
                "voice_revision_id": None,
                "fencing_token": 1,
                "command_digest": DIGEST,
                "command": {"schema_version": "1.0.0", "kind": "tts"},
                "deadline_at": "2026-10-27T00:00:00Z",
                "max_output_audio_ms": 1000,
                "sample_rate_hz": 24000,
            },
        )


class Publisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Mapping[str, Any]]] = []

    async def publish(
        self, job: DispatchJob, submission: ExecutionSubmission, receipt: Mapping[str, Any]
    ) -> PublishedResult:
        self.calls.append((job.job_id, submission.submit_intent_id, receipt))
        return PublishedResult("artifact-synthetic", "receipt-synthetic")


class GatewayState:
    def __init__(
        self,
        *,
        terminal: str = "succeeded",
        error_code: str | None = None,
        queries_before_terminal: int = 1,
        first_submit_unknown: bool = False,
        submit_status: int = 200,
        response_submit_id: str | None = None,
    ) -> None:
        self.terminal = terminal
        self.error_code = error_code
        self.queries_before_terminal = queries_before_terminal
        self.first_submit_unknown = first_submit_unknown
        self.submit_status = submit_status
        self.response_submit_id = response_submit_id
        self.submit_calls = 0
        self.query_calls = 0
        self.last_body: dict[str, Any] | None = None

    def status(self, state: str) -> dict[str, Any]:
        observed = (
            "submit-" + "b" * 32 if self.last_body is None else self.last_body["submit_intent_id"]
        )
        document: dict[str, Any] = {
            "submit_intent_id": self.response_submit_id or observed,
            "execution_state": state,
        }
        if self.error_code is not None:
            document["error_code"] = self.error_code
        return document

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/internal/v1/executions/submit":
            self.submit_calls += 1
            self.last_body = json.loads(request.content)
            if self.first_submit_unknown and self.submit_calls == 1:
                return httpx.Response(
                    503,
                    json={
                        "error": {
                            "code": "control_outcome_unknown",
                            "message": "unknown",
                            "retryable": True,
                            "recovery": "query_original",
                        }
                    },
                )
            if self.submit_status != 200:
                return httpx.Response(
                    self.submit_status,
                    json={"error": {"code": "component_credential_invalid", "message": "no"}},
                )
            return httpx.Response(200, json=self.status("executing"))
        if request.method == "GET" and path.startswith("/internal/v1/executions/"):
            submit_id = path.split("/")[-1]
            if submit_id in ("staging-result", "audio-staging-result"):
                submit_id = path.split("/")[-2]
            if self.submit_calls == 0:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "control_execution_not_found",
                            "message": "missing",
                        }
                    },
                )
            if path.endswith("/staging-result") or path.endswith("/audio-staging-result"):
                return httpx.Response(
                    200,
                    json={
                        "submit_intent_id": submit_id,
                        "request_digest": DIGEST,
                        "staging_result": {
                            "request_digest": DIGEST,
                            "reference": {
                                "asset_or_artifact_id": "artifact-staging",
                                "workspace_id": "workspace-default",
                                "storage_bucket": "synthetic",
                                "object_key": "synthetic/result",
                                "content_sha256": DIGEST,
                                "byte_length": 32,
                            },
                            "storage_version": "version-1",
                            "media_type": "audio/wav",
                        },
                    },
                )
            self.query_calls += 1
            if self.query_calls <= self.queries_before_terminal:
                return httpx.Response(200, json=self.status("executing"))
            return httpx.Response(200, json=self.status(self.terminal))
        raise AssertionError("unexpected gateway path: " + request.method + " " + path)


def _dispatcher(
    queue: Queue,
    states: States,
    gateway_state: GatewayState,
    publisher: Publisher,
    *,
    now: list[int] | None = None,
    sleep: Any = None,
    limits: DispatchLimits | None = None,
) -> JobDispatcher:
    transport = httpx.MockTransport(gateway_state.handler)
    gateway = HttpExecutionGateway(
        ExecutionGatewayEndpoint("http://127.0.0.1:9999", "synthetic-token-0001"),
        timeout_seconds=1,
        _transport=transport,
    )
    return JobDispatcher(
        queue,
        states,
        Planner(),
        gateway,
        publisher,
        limits=limits or DispatchLimits(poll_seconds=0.01, job_timeout_seconds=10),
        now_us=(lambda: (now or [NOW])[0]),
        sleep=sleep or (lambda _: asyncio.sleep(0)),
    )


async def test_claimed_job_runs_to_success_and_keeps_a_reference_only() -> None:
    job = _job()
    states = States(job)
    gateway = GatewayState(queries_before_terminal=1)
    publisher = Publisher()
    dispatcher = _dispatcher(
        Queue([[ClaimedJob(job, "worker-synthetic", 1, NOW + 30_000_000)]]),
        states,
        gateway,
        publisher,
    )

    batch = await dispatcher.run_once("worker-synthetic")

    current = states.jobs[job.job_id]
    assert batch.succeeded == 1 and gateway.submit_calls == 1
    assert current.state == "succeeded" and current.status_version == 3
    assert states.events == [
        (job.job_id, 1, "running"),
        (job.job_id, 2, "succeeded"),
    ]
    assert publisher.calls and publisher.calls[0][0] == job.job_id


async def test_reclaim_after_a_crash_queries_the_original_identity_once() -> None:
    job = _job()
    states = States(job)
    gateway = GatewayState(queries_before_terminal=10)

    async def crash(_: float) -> None:
        raise asyncio.CancelledError

    first = _dispatcher(
        Queue([[ClaimedJob(job, "worker-synthetic", 1, NOW + 30_000_000)]]),
        states,
        gateway,
        Publisher(),
        sleep=crash,
    )
    with pytest.raises(asyncio.CancelledError):
        await first.run_once("worker-synthetic")
    assert states.jobs[job.job_id].state == "running"

    gateway.queries_before_terminal = 0
    running = states.jobs[job.job_id]
    second = _dispatcher(
        Queue([[ClaimedJob(running, "worker-synthetic", 2, NOW + 60_000_000)]]),
        states,
        gateway,
        Publisher(),
    )
    batch = await second.run_once("worker-synthetic")

    assert batch.succeeded == 1
    assert gateway.submit_calls == 1
    assert states.jobs[job.job_id].state == "succeeded"


async def test_unknown_submit_is_queried_instead_of_retried_as_failure() -> None:
    job = _job()
    states = States(job)
    gateway = GatewayState(queries_before_terminal=0, first_submit_unknown=True)
    publisher = Publisher()
    first = _dispatcher(
        Queue([[ClaimedJob(job, "worker-synthetic", 1, NOW + 30_000_000)]]),
        states,
        gateway,
        publisher,
    )

    batch = await first.run_once("worker-synthetic")
    assert batch.unknown == 1 and batch.failed == 0
    assert states.jobs[job.job_id].state == "running"

    running = states.jobs[job.job_id]
    second = _dispatcher(
        Queue([[ClaimedJob(running, "worker-synthetic", 2, NOW + 60_000_000)]]),
        states,
        gateway,
        publisher,
    )
    batch = await second.run_once("worker-synthetic")
    assert batch.succeeded == 1
    assert gateway.submit_calls == 1
    assert states.jobs[job.job_id].state == "succeeded"


async def test_mismatched_status_identity_is_unknown_not_terminal() -> None:
    job = _job()
    states = States(job)
    gateway = GatewayState(
        queries_before_terminal=0,
        response_submit_id="submit-" + "c" * 32,
    )
    dispatcher = _dispatcher(
        Queue([[ClaimedJob(job, "worker-synthetic", 1, NOW + 30_000_000)]]),
        states,
        gateway,
        Publisher(),
    )

    batch = await dispatcher.run_once("worker-synthetic")

    assert batch.unknown == 1 and batch.failed == 0
    assert gateway.submit_calls == 1
    assert states.jobs[job.job_id].state == "running"


async def test_gateway_failure_maps_to_the_frozen_error_code() -> None:
    job = _job()
    states = States(job)
    gateway = GatewayState(
        terminal="failed",
        error_code="engine_transport_unknown",
        queries_before_terminal=0,
    )
    dispatcher = _dispatcher(
        Queue([[ClaimedJob(job, "worker-synthetic", 1, NOW + 30_000_000)]]),
        states,
        gateway,
        Publisher(),
    )

    batch = await dispatcher.run_once("worker-synthetic")

    assert batch.failed == 1
    assert states.jobs[job.job_id].state == "failed"
    assert batch.succeeded == 0


async def test_credential_rejection_is_terminal_not_unknown() -> None:
    job = _job()
    states = States(job)
    gateway = GatewayState(submit_status=401)
    dispatcher = _dispatcher(
        Queue([[ClaimedJob(job, "worker-synthetic", 1, NOW + 30_000_000)]]),
        states,
        gateway,
        Publisher(),
    )

    batch = await dispatcher.run_once("worker-synthetic")

    assert batch.failed == 1 and batch.unknown == 0
    assert states.jobs[job.job_id].state == "failed"


async def test_job_timeout_is_bounded_even_while_gateway_keeps_executing() -> None:
    job = _job()
    states = States(job)
    gateway = GatewayState(queries_before_terminal=1_000)
    clock = [NOW]

    async def advance(_: float) -> None:
        clock[0] += 2_000_000

    dispatcher = _dispatcher(
        Queue([[ClaimedJob(job, "worker-synthetic", 1, NOW + 30_000_000)]]),
        states,
        gateway,
        Publisher(),
        now=clock,
        sleep=advance,
        limits=DispatchLimits(poll_seconds=0.01, job_timeout_seconds=1),
    )

    batch = await dispatcher.run_once("worker-synthetic")

    assert batch.unknown == 1 and batch.failed == 0
    assert states.jobs[job.job_id].state == "running"


def test_submit_intent_identity_is_stable_and_distinct_per_job() -> None:
    assert submit_intent_identity("job-synthetic-0001") == submit_intent_identity(
        "job-synthetic-0001"
    )
    assert submit_intent_identity("job-synthetic-0001") != submit_intent_identity(
        "job-synthetic-0002"
    )


async def test_running_job_at_attempt_limit_is_unknown_not_failed() -> None:
    job = _job(state="running", version=2)
    states = States(job)
    gateway = GatewayState()
    dispatcher = _dispatcher(
        Queue([[ClaimedJob(job, "worker-synthetic", 6, NOW + 30_000_000)]]),
        states,
        gateway,
        Publisher(),
        limits=DispatchLimits(poll_seconds=0.01, job_timeout_seconds=10, max_attempts=5),
    )

    batch = await dispatcher.run_once("worker-synthetic")

    assert batch.unknown == 1 and batch.failed == 0
    assert states.jobs[job.job_id].state == "running"
    assert gateway.submit_calls == 0


async def test_attempt_limit_fails_without_calling_the_gateway() -> None:
    job = _job()
    states = States(job)
    gateway = GatewayState()
    dispatcher = _dispatcher(
        Queue([[ClaimedJob(job, "worker-synthetic", 6, NOW + 30_000_000)]]),
        states,
        gateway,
        Publisher(),
        limits=DispatchLimits(poll_seconds=0.01, job_timeout_seconds=10, max_attempts=5),
    )

    batch = await dispatcher.run_once("worker-synthetic")

    assert batch.failed == 1 and gateway.submit_calls == 0
    assert states.jobs[job.job_id].state == "failed"
