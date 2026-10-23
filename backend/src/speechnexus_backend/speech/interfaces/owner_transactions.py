"""Validated owner port boundary; no HTTP route or acquisition retry policy."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.application.owners import OwnerService
from speechnexus_backend.speech.domain.owners import (
    AcquireOwner,
    ActivateOwner,
    OwnerAuthority,
    OwnerResult,
    RenewOwner,
)


class OwnerTransactionBoundary:
    def __init__(self, service: OwnerService) -> None:
        self._service = service

    def acquire_owner(self, request: Any) -> dict[str, Any]:
        command = self._command(
            "acquire_owner",
            request,
            lambda: AcquireOwner(
                request["pool_id"],
                request["owner_id"],
                int(request["expected_version"]),
                int(request["ttl_ms"]),
            ),
        )
        return self._wire("acquire_owner", self._service.acquire(command))

    def renew_owner(self, request: Any) -> dict[str, Any]:
        command = self._command(
            "renew_owner",
            request,
            lambda: RenewOwner(
                self._authority(request["authority"]),
                int(request["expected_version"]),
                int(request["ttl_ms"]),
            ),
        )
        return self._wire("renew_owner", self._service.renew(command))

    def activate_owner(self, request: Any) -> dict[str, Any]:
        command = self._command(
            "activate_owner",
            request,
            lambda: ActivateOwner(
                self._authority(request["authority"]),
                int(request["expected_version"]),
            ),
        )
        return self._wire("activate_owner", self._service.activate(command))

    def query_outcome(self, request: Any) -> dict[str, Any]:
        validate_coordination("query_outcome", request)
        if request["target"]["kind"] != "pool":
            raise ContractValidationError("Owner boundary only queries pool authority")
        return self._wire("query_outcome", self._service.query(request["target"]["pool_id"]))

    @staticmethod
    def _authority(value: Any) -> OwnerAuthority:
        # JSON Schema integer includes integral JSON numbers. Translate their
        # representation into a domain int after validation without mutating DTOs.
        return OwnerAuthority(value["pool_id"], value["owner_id"], int(value["fencing_token"]))

    @staticmethod
    def _command[T](operation: str, request: Any, factory: Callable[[], T]) -> T:
        validate_coordination(operation, request)
        try:
            return factory()
        except ValueError:
            raise ContractValidationError(f"Invalid {operation}") from None

    @staticmethod
    def _wire(operation: str, result: OwnerResult) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema_version": "1.0.0",
            "operation": operation,
            "outcome": result.outcome,
            "scope_id": result.pool_id,
            "recovery": result.recovery,
        }
        if result.fact is not None:
            fact = result.fact
            wire["version"] = str(fact.version)
            wire["value"] = {
                "kind": "owner",
                "authority": {
                    "pool_id": fact.pool_id,
                    "owner_id": fact.owner_id,
                    "fencing_token": fact.fencing_token,
                },
                "expires_at_us": fact.expires_at_us,
                "owner_state": fact.owner_state,
                "version": str(fact.version),
            }
        if result.error_code is not None:
            wire["error_code"] = result.error_code
        validate_coordination("TransactionResult", wire)
        return wire
