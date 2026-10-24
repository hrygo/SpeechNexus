"""Runtime transactions own their complete short unit; unknowns only query originals."""

from typing import Protocol

from speechnexus_backend.speech.domain.runtime_intents import (
    CreateRuntimeIntent,
    RuntimeIdentity,
    RuntimeResult,
)
from speechnexus_backend.speech.domain.runtime_observations import ObserveRuntimeIntent


class RuntimeTransactions(Protocol):
    def observe_runtime(self, command: ObserveRuntimeIntent) -> RuntimeResult: ...

    def create_runtime_intent(self, command: CreateRuntimeIntent) -> RuntimeResult: ...

    def query_runtime_intent(
        self, identity: RuntimeIdentity, *, request_digest: str | None = None
    ) -> RuntimeResult: ...


class RuntimeService:
    def __init__(self, transactions: RuntimeTransactions) -> None:
        self._transactions = transactions

    def create(self, command: CreateRuntimeIntent) -> RuntimeResult:
        return self._transactions.create_runtime_intent(command)

    def observe(self, command: ObserveRuntimeIntent) -> RuntimeResult:
        return self._transactions.observe_runtime(command)

    def query(
        self, identity: RuntimeIdentity, *, request_digest: str | None = None
    ) -> RuntimeResult:
        return self._transactions.query_runtime_intent(identity, request_digest=request_digest)
