"""Trusted deletion-coordinator boundary; intentionally absent from HTTP composition."""

from typing import Any

from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.application.redact_execution_command import (
    ExecutionCommandRedactionService,
)
from speechnexus_gateway.domain.command_redaction import RedactExecutionCommand
from speechnexus_gateway.interfaces.execution_transactions import execution_transaction_result


class ExecutionCommandRedactionBoundary:
    def __init__(self, service: ExecutionCommandRedactionService) -> None:
        self._service = service

    def redact_execution_command(self, request: Any) -> dict[str, Any]:
        validate_coordination("redact_execution_command", request)

        def redact() -> tuple[bool, int, dict[str, object]]:
            result = self._service.redact_execution_command(
                RedactExecutionCommand(
                    request["submit_intent_id"],
                    int(request["expected_version"]),
                    request["deletion_job_id"],
                    request["deletion_evidence_digest"],
                )
            )
            return (
                result.replayed,
                result.intent.version,
                {
                    "kind": "fact",
                    "fact_id": result.intent.identity.submit_intent_id,
                    "request_digest": result.intent.request_digest,
                },
            )

        return execution_transaction_result(
            "redact_execution_command", request["submit_intent_id"], redact
        )
