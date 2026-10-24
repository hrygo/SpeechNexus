"""One complete recipe through a single transaction port; no compensation chain."""

from typing import Protocol

from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionResult,
    AdmitRecipe,
)


class AdmissionTransactions(Protocol):
    def admit_recipe(self, command: AdmitRecipe) -> AdmissionResult: ...

    def query_admission(
        self, identity: AdmissionIdentity, *, request_digest: str | None = None
    ) -> AdmissionResult: ...


class AdmissionService:
    def __init__(self, transactions: AdmissionTransactions) -> None:
        self._transactions = transactions

    def admit(self, command: AdmitRecipe) -> AdmissionResult:
        return self._transactions.admit_recipe(command)

    def query(
        self, identity: AdmissionIdentity, *, request_digest: str | None = None
    ) -> AdmissionResult:
        return self._transactions.query_admission(identity, request_digest=request_digest)
