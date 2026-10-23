"""Narrow owner ports; unknown outcomes never trigger another mutation."""

from typing import Protocol

from speechnexus_backend.speech.domain.owners import (
    AcquireOwner,
    ActivateOwner,
    OwnerResult,
    RenewOwner,
)


class OwnerTransactions(Protocol):
    def acquire_owner(self, request: AcquireOwner) -> OwnerResult: ...

    def renew_owner(self, request: RenewOwner) -> OwnerResult: ...

    def activate_owner(self, request: ActivateOwner) -> OwnerResult: ...

    def query_owner(self, pool_id: str) -> OwnerResult: ...


class OwnerService:
    def __init__(self, transactions: OwnerTransactions) -> None:
        self._transactions = transactions

    def acquire(self, request: AcquireOwner) -> OwnerResult:
        return self._transactions.acquire_owner(request)

    def renew(self, request: RenewOwner) -> OwnerResult:
        return self._transactions.renew_owner(request)

    def activate(self, request: ActivateOwner) -> OwnerResult:
        return self._transactions.activate_owner(request)

    def query(self, pool_id: str) -> OwnerResult:
        return self._transactions.query_owner(pool_id)
