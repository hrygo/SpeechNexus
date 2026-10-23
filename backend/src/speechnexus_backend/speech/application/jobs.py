"""Narrow atomic Job/outbox port; unknown outcomes never trigger a new write."""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.speech.domain.jobs import CreateJob, JobResult


class JobTransactions(Protocol):
    def create_job(self, request: CreateJob) -> JobResult:
        """Commit one Job and initial outbox fact, or return the original identity."""
        ...

    def recover_create_job(self, request: CreateJob) -> JobResult:
        """Read the original creation facts only; absence is still unknown."""
        ...


class CreateJobService:
    def __init__(self, transactions: JobTransactions) -> None:
        self._transactions = transactions

    def create(self, request: CreateJob) -> JobResult:
        # Only the adapter may retry a transaction the DB has confirmed aborted.
        # In particular, an unknown result is returned unchanged to the caller.
        return self._transactions.create_job(request)

    def recover(self, request: CreateJob) -> JobResult:
        return self._transactions.recover_create_job(request)
