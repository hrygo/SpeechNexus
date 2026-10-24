"""Lease isolation and original-state queries; no engine stop or dispatch authority."""

from typing import Protocol

from speechnexus_backend.speech.domain.leases import LeaseResult, QuarantineLease


class LeaseTransactions(Protocol):
    def quarantine_lease(self, request: QuarantineLease) -> LeaseResult: ...

    def query_lease(self, lease_id: str) -> LeaseResult: ...


class LeaseService:
    def __init__(self, transactions: LeaseTransactions) -> None:
        self._transactions = transactions

    def quarantine(self, request: QuarantineLease) -> LeaseResult:
        return self._transactions.quarantine_lease(request)

    def query(self, lease_id: str) -> LeaseResult:
        return self._transactions.query_lease(lease_id)
