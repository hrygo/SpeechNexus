"""Confirm an accepted public Job cancellation against its original execution.

The durable acceptance lives in the Job store. This module performs at most one
bounded stop attempt and closes cancelling -> cancelled only when the external
observation is a verified stop. Every other observation remains cleanup-pending.

A verified stop is also the authority to return capacity, so the same
confirmation releases the Job's admission lease. That release is idempotent and
must never undo the committed terminal state: a refused or failed release leaves
the lease reserved as the durable retry marker and reports ``False`` instead of
raising into the HTTP answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from speechnexus_backend.speech.application.public_jobs import (
    IdempotencyConflictError,
    JobOutcomeUnknownError,
    PublicJobs,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobCancellationReceipt,
    PublicJob,
)

_LOG = logging.getLogger(__name__)

StopState = Literal["stopped", "failed", "unconfirmed", "not_found", "unknown"]


@dataclass(frozen=True, slots=True)
class StopObservation:
    state: StopState
    error_code: str | None = None


# Returns True only when the lease is durably released (or already was).
CapacityRelease = Callable[[str], bool]


class CancellationCapacity:
    """The single idempotent lease release behind a confirmed cancellation.

    Every path that owns a confirmed cancellation holds this same object, so the
    retry and failure rule cannot apply to only one of them: the HTTP
    confirmation and the reconciliation settlement port both call
    :meth:`release`. Release is idempotent in ``LeaseService``; a Job that is
    already ``cancelled`` retries it, which is how a refused or unknown release is
    retried by the next pass.

    A refused or failed release keeps the lease held -- that reserved row *is* the
    durable marker a later retry finds -- and reports ``False``. It never raises:
    the terminal ``cancelled`` state is already committed, so a capacity failure
    must not roll it back or turn the cancellation into a server error. The broad
    catch keeps that answer retryable and logs the failure with a traceback instead
    of dropping the only chance to return capacity.
    """

    def __init__(self, release_capacity: CapacityRelease) -> None:
        self._release_capacity = release_capacity

    def release(self, job_id: str) -> bool:
        # The default deployment runs no cancellation worker, so a swallowed
        # failure would leave the lease held forever while the API reports the Job
        # as fully cancelled. Keep the answer a retryable False, but never silently:
        # a programming error here must be visible in the logs (#852 review).
        try:
            released = self._release_capacity(job_id)
        except Exception:
            _LOG.warning(
                "cancellation capacity release raised",
                extra={"job_id": job_id},
                exc_info=True,
            )
            return False
        if not released:
            _LOG.warning("cancellation capacity release deferred", extra={"job_id": job_id})
        return released

    async def release_async(self, job_id: str) -> bool:
        """Run the blocking lease transaction in a worker thread.

        The port is synchronous and may wait for a row lock for up to its
        transaction budget. The HTTP confirmation runs on the event loop, so it
        uses this form instead of blocking request handling.
        """
        return await asyncio.to_thread(self.release, job_id)


class JobStopper(Protocol):
    async def stop(self, job: PublicJob, receipt: JobCancellationReceipt) -> StopObservation: ...


class JobCancellationService:
    """Translate one verified stop into the persisted cancelled terminal state."""

    def __init__(
        self, jobs: PublicJobs, stopper: JobStopper, capacity: CancellationCapacity
    ) -> None:
        self._jobs, self._stopper, self._capacity = jobs, stopper, capacity

    async def confirm(self, job: PublicJob, receipt: JobCancellationReceipt) -> PublicJob:
        if job.state != "cancelling" or receipt.job_id != job.job_id:
            return job
        observation = await self._stopper.stop(job, receipt)
        if observation.state != "stopped":
            # Not-found is not stop evidence and a failed execution is not a
            # cancellation; both stay cleanup-pending for later reconciliation.
            return job
        try:
            settled = self._jobs.settle_cancelled(job.job_id, expected_version=job.status_version)
        except IdempotencyConflictError, JobOutcomeUnknownError:
            return self._jobs.get(job.job_id)
        if settled.state == "cancelled":
            # The stop is verified and the terminal state is committed, so return
            # the lease here: the default deployment runs no cancellation
            # reconciliation worker, and waiting for one would hold the slot
            # forever. A failed release keeps the Job cancelled and the lease
            # reserved for the next retry; it is never surfaced as a 5xx.
            await self._capacity.release_async(job.job_id)
        return settled


__all__ = [
    "CancellationCapacity",
    "CapacityRelease",
    "JobCancellationService",
    "JobStopper",
    "StopObservation",
]
