"""Drive claimed public Jobs through the gateway without inventing outcomes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from speechnexus_backend.speech.domain.job_dispatch import (
    DISPATCHABLE_JOB_KINDS,
    VOICE_JOB_KINDS,
    VOICE_VALIDATION_JOB_KINDS,
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
    generated_audio_ms,
    identifier,
    submit_intent_identity,
    terminal_result,
    validate_voice_dispatch_request,
)


class DispatchQueue(Protocol):
    async def claim_due(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]: ...


class DispatchStateStore(Protocol):
    async def reload(self, job_id: str) -> DispatchJob: ...

    async def mark_running(self, job: DispatchJob) -> DispatchJob: ...

    async def renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob | None: ...

    async def record_cancellation_not_submitted(self, job_id: str, *, observer: str) -> None: ...

    async def record_progress(
        self, claim: ClaimedJob, *, unit: str, completed: int, total: int | None
    ) -> bool: ...

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


_LOG = logging.getLogger(__name__)


class LeaseSettlement(Protocol):
    """Return the admission lease of a terminal asr/tts Job.

    Implementations delegate to ``LeaseService.release``; the dispatcher only
    names the operation it settled. ``True`` means the lease is released or was
    already released; ``False`` leaves it held for reconciliation.
    """

    async def release(self, operation_id: str) -> bool: ...


@dataclass(slots=True)
class _DispatchPhase:
    """Mutable markers for gateway protocol handling and an actual submit.

    ``submitted`` only says a send may have happened (or an execution was already
    observed); ``absent`` records an explicit gateway answer that no execution
    exists. The cancellation not-submitted fact requires one of: the Job was
    never marked running, or ``absent`` was proven by the gateway.
    """

    gateway: bool = False
    submitted: bool = False
    absent: bool = False


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
        self._leases: LeaseSettlement | None = None

    @property
    def limits(self) -> DispatchLimits:
        return self._limits

    def settle_capacity_through(self, leases: LeaseSettlement) -> None:
        """Compose the lease service after construction; absence keeps today's behavior."""
        self._leases = leases

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
            # A not-submitted observation may only be written when this dispatch
            # provably never sent anything: the Job was never marked running, or
            # the gateway explicitly answered that the identity has no execution.
            # An already-observed execution, a lost query or a lost submit answer
            # must never become durable "not submitted" evidence.
            if phase.absent or (not phase.submitted and claim.job.state == "pending"):
                await self._record_not_submitted_if_cancelling(claim, job.job_id)

    async def _dispatch_claimed(
        self,
        claim: ClaimedJob,
        should_stop: Callable[[], bool] | None,
        phase: _DispatchPhase,
    ) -> DispatchResult:
        job = claim.job
        # Remembered before mark_running rewrites the claim: a claim that started
        # pending owns the first attempt, so no earlier worker can have submitted.
        claimed_running = job.state == "running"
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
            # Resolve a voice Job's result reference before any side effect: a
            # historical row whose stored command lacks the matching kind must
            # fail here, not after publishing an orphan Artifact.
            validate_voice_dispatch_request(job)
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
            try:
                reply = await self._gateway.query(submission.submit_intent_id)
            except DispatchUnavailableError:
                # An unreadable query may hide a previous submit. This dispatch
                # must never become a durable "not submitted" observation.
                phase.submitted = True
                raise
            if reply.outcome != "not_found":
                # The gateway already reports an execution, or cannot say there
                # is none. That answer may be the submit of a crashed worker, so
                # this dispatch is not a pre-submit exit.
                phase.submitted = True
            if reply.outcome == "not_found":
                phase.absent = True
                renewed = await self._states.renew(claim, self._limits.lease_ms)
                if renewed is None:
                    return DispatchResult(job.job_id, "claim_lost", "claim_lost")
                claim = renewed
                if self._submission_expired(submission):
                    # The gateway answered 404 for the stable identity and the
                    # frozen deadline has passed, so it can no longer accept this
                    # submit. That pair is the durable not-submitted fact; only
                    # then may the Job fail and its capacity return.
                    return await self._settle_not_submitted(claim, job)
                if self._expired(job):
                    # Dispatcher-side expiry is not engine evidence: the Job
                    # stays running and reconciliation settles it from durable
                    # execution facts. Only an engine-reported failed/aborted
                    # outcome may become a public failure.
                    return DispatchResult(job.job_id, "unknown", "job_timeout")
                # The send may be accepted even if its response is lost.
                phase.absent = False
                phase.submitted = True
                reply = await self._gateway.submit(submission)
            return await self._follow(claim, job, submission, reply, should_stop, phase)
        except DispatchRequestUnavailableError:
            return await self._fail_absent_execution(
                claim, job, "job_request_unavailable", phase, claimed_running=claimed_running
            )
        except DispatchRejectedError as error:
            if error.error_code == "gateway_barrier_fenced" and await self._cancelling(job):
                # The gateway's barrier refused this submit and the Job is already
                # cancelling: no execution exists. The finally writes the durable
                # no-execution fact for reconciliation.
                phase.absent = True
                return DispatchResult(job.job_id, "skipped", "gateway_barrier_fenced")
            return await self._fail(claim, job, error.error_code)
        except DispatchConflictError:
            return DispatchResult(job.job_id, "claim_lost", "claim_lost")
        except DispatchProtocolError:
            if phase.gateway:
                # An unreadable or mismatched gateway reply never proves failure.
                return DispatchResult(job.job_id, "unknown", "outcome_unknown")
            return await self._fail_absent_execution(
                claim, job, "execution_result_invalid", phase, claimed_running=claimed_running
            )
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
        reported_ms = 0
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
                phase.absent = False
                phase.submitted = True
                reply = await self._gateway.submit(submission)
                submitted = reply.outcome == "status"
                continue
            if reply.outcome == "rejected":
                if reply.error_code == "gateway_barrier_fenced" and await self._cancelling(job):
                    phase.absent = True
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
            if job.kind == "tts":
                observed_ms = generated_audio_ms(status)
                if observed_ms is not None and observed_ms > reported_ms:
                    try:
                        await self._states.record_progress(
                            claim, unit="audio_ms", completed=observed_ms, total=None
                        )
                    except DispatchUnavailableError:
                        # Progress is an observation, not an execution outcome. A
                        # storage hiccup must not turn a healthy execution into
                        # unknown; the next poll retries the same durable watermark.
                        pass
                    else:
                        # A false result still means the stored fact is not behind
                        # this observation; do not re-ask for the same value.
                        reported_ms = observed_ms
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
            submission.submit_intent_id,
            audio=job.kind == "tts" or job.kind in VOICE_JOB_KINDS,
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
        result_json = (
            published.result_json
            if published.result_json is not None
            else terminal_result(job, published.artifact_id, published.receipt_id)
        )
        return await self._finish(claim, job, "succeeded", result_json=result_json, error_code=None)

    async def _fail(self, claim: ClaimedJob, job: DispatchJob, code: str) -> DispatchResult:
        return await self._finish(claim, job, "failed", result_json=None, error_code=code)

    async def _fail_absent_execution(
        self,
        claim: ClaimedJob,
        job: DispatchJob,
        code: str,
        phase: _DispatchPhase,
        *,
        claimed_running: bool,
    ) -> DispatchResult:
        """Never fail a previously submitted Job without a gateway 404.

        A historical row that was already claimed may have been submitted by a
        crashed worker. A stored-document disagreement then says nothing about
        the execution, so the Job stays ``running`` unless the gateway itself
        confirms there is no execution for the stable ``submit_intent_id``. An
        unreadable or executing answer may hide a previous submit and must not
        become a durable "not submitted" fact.
        """
        if not claimed_running:
            # mark_running precedes every external submit. A claim that started
            # pending owns the first attempt and cannot have a send from a
            # crashed worker, so no gateway confirmation is needed here.
            return await self._fail(claim, job, code)
        try:
            reply = await self._gateway.query(submit_intent_identity(job.job_id))
        except DispatchUnavailableError, DispatchProtocolError:
            phase.submitted = True
            return DispatchResult(job.job_id, "unknown", "outcome_unknown")
        if reply.outcome != "not_found":
            phase.submitted = True
            return DispatchResult(job.job_id, "unknown", "outcome_unknown")
        # The gateway explicitly confirmed that this identity has no execution;
        # that answer is durable evidence even when the claim started running.
        phase.absent = True
        return await self._fail(claim, job, code)

    async def _settle_not_submitted(self, claim: ClaimedJob, job: DispatchJob) -> DispatchResult:
        """Persist the durable not-submitted terminal and then return capacity."""
        return await self._finish(
            claim,
            job,
            "failed",
            result_json=None,
            error_code="execution_deadline_expired",
        )

    async def _release_capacity(self, job: DispatchJob) -> None:
        """Admitted asr/tts/validation terminals return the lease through LeaseService.

        A release that cannot prove stop and cleanup stays held and is left for
        reconciliation; it never rewrites the committed public outcome.
        """
        if self._leases is None or job.kind not in (
            *DISPATCHABLE_JOB_KINDS,
            *VOICE_VALIDATION_JOB_KINDS,
        ):
            return
        try:
            released = await self._leases.release(job.job_id)
        except Exception:
            _LOG.warning("lease release raised", extra={"job_id": job.job_id})
            return
        if released is not True:
            # The lease row itself is the durable marker; operators find it via
            # the terminal Job with a still-held lease and settle by original id.
            _LOG.warning("lease release deferred to reconciliation", extra={"job_id": job.job_id})

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
            await self._release_capacity(job)
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

    def _submission_expired(self, submission: ExecutionSubmission) -> bool:
        """Only the frozen admission deadline, never the dispatcher's own timer."""
        value = submission.body.get("deadline_at")
        if not isinstance(value, str):
            return False
        try:
            deadline = datetime.fromisoformat(value)
        except ValueError:
            return False
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            return False
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        return self._now_us() >= (deadline - epoch) // timedelta(microseconds=1)


__all__ = [
    "DispatchPlanner",
    "DispatchQueue",
    "DispatchStateStore",
    "ExecutionGateway",
    "JobDispatcher",
    "LeaseSettlement",
    "ResultPublisher",
]
