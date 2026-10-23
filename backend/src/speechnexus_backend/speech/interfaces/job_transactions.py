"""Validated process-local Job boundary; does not create an HTTP service."""

from __future__ import annotations

from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.application.jobs import CreateJobService
from speechnexus_backend.speech.domain.jobs import CreateJob, JobResult


class JobTransactionBoundary:
    def __init__(self, service: CreateJobService) -> None:
        self._service = service

    def create_job(self, request: Any) -> dict[str, Any]:
        return self._wire(self._service.create(self._command(request)))

    def recover_create_job(self, request: Any) -> dict[str, Any]:
        return self._wire(self._service.recover(self._command(request)))

    @staticmethod
    def _command(request: Any) -> CreateJob:
        validate_coordination("create_job", request)
        try:
            return CreateJob(
                job_id=request["job"]["job_id"],
                operation_id=request["identity"]["operation_id"],
                attempt_id=request["identity"]["attempt_id"],
                event_id=request["job"]["event_id"],
                event_digest=request["job"]["event_digest"],
                request_digest=request["request_digest"],
            )
        except ValueError:
            raise ContractValidationError("Invalid create_job") from None

    @staticmethod
    def _wire(result: JobResult) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema_version": "1.0.0",
            "operation": "create_job",
            "outcome": result.outcome,
            "scope_id": result.job_id,
            "recovery": result.recovery,
        }
        if result.fact is not None:
            wire["value"] = {
                "kind": "fact",
                "fact_id": result.fact.job_id,
                "request_digest": result.fact.request_digest,
            }
        if result.error_code is not None:
            wire["error_code"] = result.error_code
        validate_coordination("TransactionResult", wire)
        return wire
