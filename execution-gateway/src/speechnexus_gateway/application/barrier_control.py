"""The authenticated boundary delegates sealing to the gateway's one shared ingress."""

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.fencing import BarrierRequest, BarrierSnapshot
from speechnexus_gateway.domain.ports import BarrierLedger
from speechnexus_gateway.domain.submission import InvalidExecutionRequestError


class GatewayBarrierService:
    def __init__(self, *, ingress: GatewayIngress, ledger: BarrierLedger) -> None:
        self._ingress = ingress
        self._ledger = ledger

    def check_ownership(self) -> None:
        self._ingress.check_ownership()

    def seal(self, gateway_id: str, request: BarrierRequest) -> BarrierSnapshot:
        if gateway_id != self._ingress.gateway_id:
            raise InvalidExecutionRequestError("the barrier targets another gateway")
        return self._ingress.seal(request, self._ledger)
