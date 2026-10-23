"""Trusted adapter observations are scoped to this gateway and never invoke engines."""

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.ports import ExecutionObservationLedger
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmissionIdentity,
    SubmissionIntent,
)


def check_gateway_scope(ingress: GatewayIngress, identity: SubmissionIdentity) -> None:
    ingress.check_ownership()
    if (identity.gateway_id, identity.host_node_id) != (ingress.gateway_id, ingress.node_id):
        raise InvalidExecutionRequestError("execution belongs to another gateway or node")


class ExecutionObservationService:
    def __init__(self, *, ingress: GatewayIngress, ledger: ExecutionObservationLedger) -> None:
        self._ingress = ingress
        self._ledger = ledger

    def record_execution_observation(self, request: RecordExecutionObservation) -> SubmissionIntent:
        check_gateway_scope(self._ingress, request.observation.identity)
        return self._ledger.record_execution_observation(request)
