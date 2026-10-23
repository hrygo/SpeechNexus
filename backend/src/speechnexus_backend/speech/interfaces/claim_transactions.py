"""Internal machine-contract boundary, separate from public Job HTTP payloads."""

from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.application.claims import ClaimService
from speechnexus_backend.speech.domain.claims import ClaimJob, ClaimResult


class ClaimTransactionBoundary:
    def __init__(self, service: ClaimService) -> None:
        self._service = service

    def claim_job(self, request: Any) -> dict[str, Any]:
        validate_coordination("claim_job", request)
        command = ClaimJob(
            request["job_id"],
            request["worker_id"],
            int(request["expected_claim_version"]),
            int(request["ttl_ms"]),
        )
        return self._wire("claim_job", self._service.claim(command))

    def query_outcome(self, request: Any) -> dict[str, Any]:
        validate_coordination("query_outcome", request)
        if request["target"]["kind"] != "job":
            raise ContractValidationError("Claim boundary only queries Job authority")
        return self._wire("query_outcome", self._service.query(request["target"]["job_id"]))

    @staticmethod
    def _wire(operation: str, result: ClaimResult) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema_version": "1.0.0",
            "operation": operation,
            "outcome": result.outcome,
            "scope_id": result.job_id,
            "recovery": result.recovery,
        }
        if result.fact is not None:
            fact = result.fact
            wire["value"] = {
                "kind": "claim",
                "job_id": fact.job_id,
                "worker_id": fact.worker_id,
                "claim_version": fact.claim_version,
                "expires_at_us": fact.expires_at_us,
            }
        if result.error_code is not None:
            wire["error_code"] = result.error_code
        validate_coordination("TransactionResult", wire)
        return wire
