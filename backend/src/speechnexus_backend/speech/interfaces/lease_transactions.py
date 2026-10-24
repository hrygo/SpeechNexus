"""Original coordination contract at the lease-isolation boundary; no HTTP route."""

from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.leases import LeaseResult, QuarantineLease


class LeaseTransactionBoundary:
    def __init__(self, service: LeaseService) -> None:
        self._service = service

    def quarantine_lease(self, request: Any) -> dict[str, Any]:
        validate_coordination("quarantine_lease", request)
        return self._wire(
            "quarantine_lease",
            self._service.quarantine(
                QuarantineLease(
                    request["lease_id"],
                    request["reason_code"],
                    request["observed_at_us"],
                )
            ),
        )

    def query_outcome(self, request: Any) -> dict[str, Any]:
        validate_coordination("query_outcome", request)
        if request["target"]["kind"] != "lease" or "request_digest" in request:
            raise ContractValidationError("Lease query requires the original lease identity")
        return self._wire("query_outcome", self._service.query(request["target"]["lease_id"]))

    @staticmethod
    def _wire(operation: str, result: LeaseResult) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema_version": "1.0.0",
            "operation": operation,
            "outcome": result.outcome,
            "scope_id": result.lease_id,
            "recovery": result.recovery,
        }
        if result.fact is not None:
            fact = result.fact
            wire["value"] = {
                "kind": "lease",
                "lease_id": fact.lease_id,
                "lease_state": fact.lease_state,
                "cleanup_state": fact.cleanup_state,
                "version": str(fact.version),
            }
        if result.error_code is not None:
            wire["error_code"] = result.error_code
        validate_coordination("TransactionResult", wire)
        return wire
