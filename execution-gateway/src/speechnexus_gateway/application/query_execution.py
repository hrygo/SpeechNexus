"""Query the recorded original identity, without engine observation or execution."""

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.ports import ExecutionQueryPort
from speechnexus_gateway.domain.recovery import ExecutionSnapshot


class ExecutionQueryService:
    def __init__(self, *, ingress: GatewayIngress, ledger: ExecutionQueryPort) -> None:
        self._ingress = ingress
        self._ledger = ledger

    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None:
        self._ingress.check_ownership()
        snapshot = self._ledger.query_outcome(submit_intent_id)
        if snapshot is None:
            return None
        identity = snapshot.intent.identity
        if (identity.gateway_id, identity.host_node_id) != (
            self._ingress.gateway_id,
            self._ingress.node_id,
        ):
            return None
        return snapshot
