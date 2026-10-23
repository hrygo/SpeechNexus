"""Runtime transactions own their complete short unit; unknowns only query originals."""

from typing import Protocol

from speechnexus_backend.speech.domain.runtime_intents import (
    CreateRuntimeIntent,
    RuntimeIdentity,
    RuntimeResult,
)


class RuntimeTransactions(Protocol):
    def create_runtime_intent(self, command: CreateRuntimeIntent) -> RuntimeResult: ...

    def query_runtime_intent(
        self, identity: RuntimeIdentity, *, request_digest: str | None = None
    ) -> RuntimeResult: ...


class RuntimeService:
    def __init__(self, transactions: RuntimeTransactions) -> None:
        self._transactions = transactions

    def create(self, command: CreateRuntimeIntent) -> RuntimeResult:
        return self._transactions.create_runtime_intent(command)

    def query(
        self, identity: RuntimeIdentity, *, request_digest: str | None = None
    ) -> RuntimeResult:
        return self._transactions.query_runtime_intent(identity, request_digest=request_digest)
