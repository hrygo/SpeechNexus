"""Require an authoritative deletion permit before invoking the narrow cleanup writer."""

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.observe_execution import check_gateway_scope
from speechnexus_gateway.domain.command_redaction import (
    CommandRedactionResult,
    RedactExecutionCommand,
)
from speechnexus_gateway.domain.ports import (
    ExecutionCommandDeletionAuthorizer,
    ExecutionCommandRedactionLedger,
)
from speechnexus_gateway.domain.submission import InvalidExecutionRequestError


class ExecutionCommandRedactionService:
    def __init__(
        self,
        *,
        ingress: GatewayIngress,
        authorizer: ExecutionCommandDeletionAuthorizer,
        ledger: ExecutionCommandRedactionLedger,
    ) -> None:
        self._ingress = ingress
        self._authorizer = authorizer
        self._ledger = ledger

    def redact_execution_command(self, request: RedactExecutionCommand) -> CommandRedactionResult:
        self._ingress.check_ownership()
        permit = self._authorizer.authorize(request)
        if permit.request != request:
            raise InvalidExecutionRequestError("deletion permit does not authorize this request")
        check_gateway_scope(self._ingress, permit.identity)
        return self._ledger.redact_execution_command(permit)
