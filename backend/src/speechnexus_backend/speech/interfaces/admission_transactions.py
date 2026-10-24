"""Frozen coordination envelope; trusted context/codecs come from composition."""

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRequest,
    AdmissionResult,
    AdmitRecipe,
    ApprovedAdmissionRecipe,
    OriginalBusinessCommand,
)


class AdmissionTransactionBoundary:
    def __init__(
        self,
        service: AdmissionService,
        *,
        decode_request: Callable[[Any, OriginalBusinessCommand], AdmissionRequest],
    ) -> None:
        self._service = service
        self._decode = decode_request

    def admit_recipe(
        self,
        request: Any,
        *,
        business_command: OriginalBusinessCommand,
        approved_recipe: ApprovedAdmissionRecipe,
    ) -> dict[str, Any]:
        validate_coordination("admit_recipe", request)
        try:
            command = AdmitRecipe(
                self._decode(request, business_command), business_command, approved_recipe
            )
        except ValueError, TypeError:
            raise ContractValidationError("Invalid admit_recipe") from None
        return self._wire("admit_recipe", self._service.admit(command))

    def query_outcome(self, request: Any) -> dict[str, Any]:
        validate_coordination("query_outcome", request)
        target = request["target"]
        if target["kind"] != "attempt":
            raise ContractValidationError(
                "Admission boundary only queries original attempt identities"
            )
        original = target["identity"]
        identity = AdmissionIdentity(original["operation_id"], original["attempt_id"])
        return self._wire(
            "query_outcome",
            self._service.query(identity, request_digest=request.get("request_digest")),
        )

    def _wire(self, operation: str, result: AdmissionResult) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema_version": "1.0.0",
            "operation": operation,
            "outcome": result.outcome,
            "scope_id": result.identity.operation_id,
            "recovery": result.recovery,
        }
        if result.fact is not None:
            fact = result.fact
            wire["version"] = str(fact.version)
            wire["value"] = {
                "kind": "admission",
                "identity": {
                    "operation_id": fact.identity.operation_id,
                    "attempt_id": fact.identity.attempt_id,
                },
                "lease_ids": [r.stage.lease_id for r in fact.reservations],
                "version": str(fact.version),
                "reservations": [
                    {
                        "lease_id": r.stage.lease_id,
                        "stage_id": r.stage.stage_id,
                        "reserved_slots": r.stage.reserved_slots,
                        "physical": asdict(r.physical),
                    }
                    for r in fact.reservations
                ],
            }
        if result.error_code is not None:
            wire["error_code"] = result.error_code
        validate_coordination("TransactionResult", wire)
        return wire
