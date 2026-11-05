"""Confirm an accepted public Job cancellation against its original execution.

The durable acceptance lives in the Job store. This module performs at most one
bounded stop attempt and closes cancelling -> cancelled only when the external
observation is a verified stop. Every other observation remains cleanup-pending.
"""

from __future__ import annotations

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

StopState = Literal["stopped", "failed", "unconfirmed", "not_found", "unknown"]


@dataclass(frozen=True, slots=True)
class StopObservation:
    state: StopState
    error_code: str | None = None


class JobStopper(Protocol):
    async def stop(self, job: PublicJob, receipt: JobCancellationReceipt) -> StopObservation: ...


class JobCancellationService:
    """Translate one verified stop into the persisted cancelled terminal state."""

    def __init__(self, jobs: PublicJobs, stopper: JobStopper) -> None:
        self._jobs, self._stopper = jobs, stopper

    async def confirm(self, job: PublicJob, receipt: JobCancellationReceipt) -> PublicJob:
        if job.state != "cancelling" or receipt.job_id != job.job_id:
            return job
        observation = await self._stopper.stop(job, receipt)
        if observation.state != "stopped":
            # Not-found is not stop evidence and a failed execution is not a
            # cancellation; both stay cleanup-pending for later reconciliation.
            return job
        try:
            return self._jobs.settle_cancelled(job.job_id, expected_version=job.status_version)
        except IdempotencyConflictError, JobOutcomeUnknownError:
            return self._jobs.get(job.job_id)


__all__ = ["JobCancellationService", "JobStopper", "StopObservation"]
