"""Narrow claim ports; acquiring worker authority never dispatches execution."""

from typing import Protocol

from speechnexus_backend.speech.domain.claims import ClaimJob, ClaimResult


class ClaimTransactions(Protocol):
    def claim_job(self, request: ClaimJob) -> ClaimResult: ...

    def query_claim(self, job_id: str) -> ClaimResult: ...


class ClaimService:
    def __init__(self, transactions: ClaimTransactions) -> None:
        self._transactions = transactions

    def claim(self, request: ClaimJob) -> ClaimResult:
        return self._transactions.claim_job(request)

    def query(self, job_id: str) -> ClaimResult:
        return self._transactions.query_claim(job_id)
