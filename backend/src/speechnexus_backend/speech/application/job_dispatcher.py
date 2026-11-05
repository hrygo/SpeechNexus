"""Drive claimed public Jobs through the gateway without inventing outcomes."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from speechnexus_backend.speech.domain.job_dispatch import (
    ClaimedJob,
    DispatchBatch,
    DispatchClaimLostError,
    DispatchConflictError,
    DispatchJob,
    DispatchLimits,
    DispatchProtocolError,
    DispatchRejectedError,
    DispatchRequestUnavailableError,
    DispatchResult,
    DispatchUnavailableError,
    ExecutionSubmission,
    GatewayReply,
    PublishedResult,
    ResultReceipt,
    gateway_failure_code,
    identifier,
    submit_intent_identity,
    terminal_result,
)


class DispatchQueue(Protocol):
    async def claim_due(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]: ...


class DispatchStateStore(Protocol):
    async def reload(self, job_id: str) -> DispatchJob: ...

    async def mark_running(self, job: DispatchJob) -> DispatchJob: ...

    async def renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob | None: ...

    async def record_cancellation_not_submitted(self, job_id: str, *, observer: str) -> None: ...

    async def finish(
        self,
        claim: ClaimedJob,
        target: str,
        *,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob: ...


class DispatchPlanner(Protocol):
    async def plan(self, job: DispatchJob, submit_intent_id: str) -> ExecutionSubmission: ...


class ExecutionGateway(Protocol):
    async def query(self, submit_intent_id: str) -> GatewayReply: ...

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply: ...

    async def result_receipt(self, submit_intent_id: str, *, audio: bool) -> ResultReceipt: ...


class ResultPublisher(Protocol):
    async def publish(
        self, job: DispatchJob, submission: ExecutionSubmission, receipt: Mapping[str, Any]
    ) -> PublishedResult: ...


@dataclass(slots=True)
class _DispatchPhase:
    """Mutable markers for gateway protocol handling and an actual submit."""

    gateway: bool = False
    submitted: bool = False


class JobDispatcher:
    """One bounded batch; the process holds no lease beyond its claim TTL."""

    def __init__(
        self,
        queue: DispatchQueue,
        states: DispatchStateStore,
        planner: DispatchPlanner,
        gateway: ExecutionGateway,
        results: ResultPublisher,
        *,
        limits: DispatchLimits | None = None,
        now_us: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._queue = queue
        self._states = states
        self._planner = planner
        self._gateway = gateway
        self._results = results
        self._limits = limits or DispatchLimits()
        self._now_us = now_us or (lambda: time.time_ns() // 1_000)
        self._sleep = sleep or asyncio.sleep

    @property
    def limits(self) -> DispatchLimits:
        return self._limits

    async def run_once(
        self, worker_id: str, *, should_stop: Callable[[], bool] | None = None
    ) -> DispatchBatch:
        identifier(worker_id)
        claims = await self._queue.claim_due(
            worker_id, self._limits.lease_ms, self._limits.batch_size
        )
        batch = DispatchBatch(
            claimed=len(claims),
            lease_reclaimed=sum(claim.claim_version > 1 for claim in claims),
        )
        for index, claim in enumerate(claims):
            if should_stop is not None and should_stop():
                for deferred in claims[index:]:
                    # The claim precedes a graceful stop; an accepted cancellation
                    # in that window is still a dispatch that must not submit.
                    await self._record_not_submitted_if_cancelling(deferred, deferred.job.job_id)
                    batch = batch.count(
                        DispatchResult(deferred.job.job_id, "deferred", "worker_stopping")
                    )
                break
            result = await self._dispatch_one(claim, should_stop)
            batch = batch.count(result)
        return batch

    async def _dispatch_one(
        self, claim: ClaimedJob, should_stop: Callable[[], bool] | None
    ) -> DispatchResult:
        job = claim.job
        phase = _DispatchPhase()
        try:
            return await self._dispatch_claimed(claim, should_stop, phase)
        finally:
            # Every exit before an actual submit is a dispatch that did not submit.
            # Observation is idempotent and covers all current/future early returns.
            if not phase.submitted:
                await self._record_not_submitted_if_cancelling(claim, job.job_id)

    async def _dispatch_claimed(
        self,
        claim: ClaimedJob,
        should_stop: Callable[[], bool] | None,
        phase: _DispatchPhase,
    ) -> DispatchResult:
        job = claim.job
        if claim.claim_version > self._limits.max_attempts:
            if job.state == "running":
                return DispatchResult(job.job_id, "unknown", "dispatch_attempts_exhausted")
            return await self._fail(claim, job, "dispatch_attempts_exhausted")
        if job.state in ("cancelling", "succeeded", "failed", "cancelled", "expired"):
            return DispatchResult(job.job_id, "skipped")
        try:
            if not job.request_document:
                raise DispatchRequestUnavailableError
            actual_digest = (
                "sha256:" + hashlib.sha256(job.request_document.encode("utf-8")).hexdigest()
            )
            if actual_digest != job.request_digest:
                raise DispatchProtocolError("Stored request disagrees with its digest")
            if job.state == "pending":
                job = await self._states.mark_running(job)
                claim = replace(claim, job=job)
            submit_intent_id = submit_intent_identity(job.job_id)
            submission = await self._planner.plan(job, submit_intent_id)
            if submission.submit_intent_id != submit_intent_id:
                raise DispatchProtocolError("Planner changed the stable execution identity")
            if should_stop is not None and should_stop():
                return DispatchResult(job.job_id, "deferred", "worker_stopping")
            renewed = await self._states.renew(claim, self._limits.lease_ms)
            if renewed is None:
                return DispatchResult(job.job_id, "claim_lost", "claim_lost")
            claim = renewed
            # A cancellation accepted after mark_running must not submit a new
            # execution. The guard is checked again inside the gateway submit
            # path by the cancellation confirmer; this closes the local window.
            current = await self._states.reload(job.job_id)
            if current.state != "running":
                return DispatchResult(job.job_id, "claim_lost", "cancellation_pending")
            phase.gateway = True
            reply = await self._gateway.query(submission.submit_intent_id)
            if reply.outcome == "not_found":
                renewed = await self._states.renew(claim, self._limits.lease_ms)
                if renewed is None:
                    return DispatchResult(job.job_id, "claim_lost", "claim_lost")
                claim = renewed
                if self._expired(job):
                    return await self._fail(claim, job, "job_timeout")
                reply = await self._gateway.submit(submission)
                phase.submitted = True
            return await self._follow(claim, job, submission, reply, should_stop, phase)
        except DispatchRequestUnavailableError:
            return await self._fail(claim, job, "job_request_unavailable")
        except DispatchRejectedError as error:
            if error.error_code == "gateway_barrier_fenced" and await self._cancelling(job):
                # The gateway's barrier refused this submit and the Job is already
                # cancelling: no execution exists. Keep cancelling and let the
                # reconciliation worker close it from the durable no-execution fact.
                return DispatchResult(job.job_id, "skipped", "gateway_barrier_fenced")
            return await self._fail(claim, job, error.error_code)
        except DispatchConflictError:
            return DispatchResult(job.job_id, "claim_lost", "claim_lost")
        except DispatchProtocolError:
            if phase.gateway:
                # An unreadable or mismatched gateway reply never proves failure.
                return DispatchResult(job.job_id, "unknown", "outcome_unknown")
            return await self._fail(claim, job, "execution_result_invalid")
        except DispatchUnavailableError:
            return DispatchResult(job.job_id, "unknown", "outcome_unknown")

    async def _cancelling(self, job: DispatchJob) -> bool:
        """Reload because acceptance can commit between the local guard and submit."""
        if job.state == "cancelling":
            return True
        current = await self._states.reload(job.job_id)
        return current.state == "cancelling"

    async def _record_not_submitted_if_cancelling(self, claim: ClaimedJob, job_id: str) -> None:
        try:
            current = await self._states.reload(job_id)
            if current.state == "cancelling":
                await self._states.record_cancellation_not_submitted(
                    job_id, observer=claim.worker_id
                )
        except DispatchUnavailableError:
            # The dispatch result is already decided. A storage outage must not
            # turn a bounded unknown/claim-lost result into an uncaught worker
            # failure; without the observation reconciliation cannot settle.
            return

    async def _follow(
        self,
        claim: ClaimedJob,
        job: DispatchJob,
        submission: ExecutionSubmission,
        reply: GatewayReply,
        should_stop: Callable[[], bool] | None,
        phase: _DispatchPhase,
    ) -> DispatchResult:
        submitted = reply.outcome == "status"
        while True:
            if should_stop is not None and should_stop():
                # Keep the lease until expiry so the next worker can recover the
                # original execution identity; never write a terminal outcome here.
                return DispatchResult(job.job_id, "deferred", "worker_stopping")
            renewed = await self._states.renew(claim, self._limits.lease_ms)
            if renewed is None:
                # Another claimant owns the row now. It may already be publishing
                # this same stable result, so this worker must not finish or publish.
                return DispatchResult(job.job_id, "claim_lost", "claim_lost")
            claim = renewed
            if reply.outcome == "unknown":
                # Query the original identity on the next claim; never create a
                # second execution for an ambiguous submission.
                return DispatchResult(job.job_id, "unknown", "outcome_unknown")
            if reply.outcome == "not_found":
                if submitted:
                    return DispatchResult(job.job_id, "unknown", "execution_not_found")
                reply = await self._gateway.submit(submission)
                phase.submitted = True
                submitted = reply.outcome == "status"
                continue
            if reply.outcome == "rejected":
                if reply.error_code == "gateway_barrier_fenced" and await self._cancelling(job):
                    return DispatchResult(job.job_id, "skipped", "gateway_barrier_fenced")
                return await self._fail(claim, job, reply.error_code or "execution_rejected")
            status = reply.status
            if status is None:
                raise DispatchProtocolError("Gateway status reply is absent")
            if status.get("submit_intent_id") != submission.submit_intent_id:
                raise DispatchProtocolError("Gateway status belongs to another execution")
            expected_digest = submission.body.get("command_digest")
            observed_digest = status.get("command_digest")
            if expected_digest is not None and observed_digest not in (None, expected_digest):
                raise DispatchProtocolError("Gateway status has another command digest")
            state = status["execution_state"]
            if state == "succeeded":
                return await self._complete(claim, job, submission, should_stop)
            if state in ("failed", "aborted"):
                return await self._fail(claim, job, gateway_failure_code(status))
            if state == "unknown_quarantined":
                return DispatchResult(job.job_id, "unknown", "outcome_unknown")
            if self._expired(job):
                # The engine may still be running: never claim a terminal outcome.
                return DispatchResult(job.job_id, "unknown", "job_timeout")
            await self._sleep(self._limits.poll_seconds)
            reply = await self._gateway.query(submission.submit_intent_id)

    async def _complete(
        self,
        claim: ClaimedJob,
        job: DispatchJob,
        submission: ExecutionSubmission,
        should_stop: Callable[[], bool] | None,
    ) -> DispatchResult:
        receipt = await self._gateway.result_receipt(
            submission.submit_intent_id, audio=job.kind == "tts"
        )
        if receipt.outcome != "status" or receipt.document is None:
            if receipt.outcome == "unknown":
                return DispatchResult(job.job_id, "unknown", "outcome_unknown")
            if receipt.outcome == "rejected":
                return await self._fail(claim, job, receipt.error_code or "result_unavailable")
            return DispatchResult(job.job_id, "unknown", "result_not_ready")
        if should_stop is not None and should_stop():
            return DispatchResult(job.job_id, "deferred", "worker_stopping")
        renewed = await self._states.renew(claim, self._limits.lease_ms)
        if renewed is None:
            return DispatchResult(job.job_id, "claim_lost", "claim_lost")
        claim = renewed
        # An accepted cancellation forbids publishing a new artifact. The
        # terminal status guard still runs in the finish transaction.
        current = await self._states.reload(job.job_id)
        if current.state != "running":
            return DispatchResult(job.job_id, "claim_lost", "cancellation_pending")
        published = await self._results.publish(job, submission, receipt.document)
        renewed = await self._states.renew(claim, self._limits.lease_ms)
        if renewed is None:
            return DispatchResult(job.job_id, "claim_lost", "claim_lost")
        claim = renewed
        result_json = terminal_result(job, published.artifact_id, published.receipt_id)
        return await self._finish(claim, job, "succeeded", result_json=result_json, error_code=None)

    async def _fail(self, claim: ClaimedJob, job: DispatchJob, code: str) -> DispatchResult:
        return await self._finish(claim, job, "failed", result_json=None, error_code=code)

    async def _finish(
        self,
        claim: ClaimedJob,
        job: DispatchJob,
        target: str,
        *,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchResult:
        try:
            current = await self._states.finish(
                claim, target, result_json=result_json, error_code=error_code
            )
        except DispatchClaimLostError:
            return DispatchResult(job.job_id, "claim_lost", "claim_lost")
        except DispatchUnavailableError:
            return DispatchResult(job.job_id, "unknown", "outcome_unknown")
        if current.state == target:
            if target == "succeeded":
                return DispatchResult(job.job_id, "succeeded", error_code)
            return DispatchResult(job.job_id, "failed", error_code)
        if current.state in ("succeeded", "failed"):
            # A concurrent claimant already settled the public Job. Do not
            # overwrite its result or claim a different outcome from this worker.
            return DispatchResult(job.job_id, "skipped")
        return DispatchResult(job.job_id, "unknown", "outcome_unknown")

    def _expired(self, job: DispatchJob) -> bool:
        limit_us = int(self._limits.job_timeout_seconds * 1_000_000)
        return self._now_us() - job.created_at_us >= limit_us


__all__ = [
    "DispatchPlanner",
    "DispatchQueue",
    "DispatchStateStore",
    "ExecutionGateway",
    "JobDispatcher",
    "ResultPublisher",
]
