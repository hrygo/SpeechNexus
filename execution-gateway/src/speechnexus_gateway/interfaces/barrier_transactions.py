"""Named full coordination result for the durable gateway barrier fact.

``seal_barrier`` records the minimum fencing token one gateway has already refused to send
below, together with the proof digest of that seal. The frozen command carries the authority
that requested the takeover, the observation the gateway made, and that proof.

This boundary records the fact: a repeated identical command returns the original fact even
after the owner that sealed it was replaced, and only a higher minimum, still authorized by
the current unexpired owner, writes a new version. It opens no HTTP route and grants no
authority of its own — a caller cannot make the gateway seal anything by asking twice.
"""

from __future__ import annotations

from typing import Any

from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.fencing import BarrierRequest, BarrierSnapshot, IngressFence
from speechnexus_gateway.domain.ports import BarrierLedger
from speechnexus_gateway.domain.submission import InvalidExecutionRequestError
from speechnexus_gateway.interfaces.execution_transactions import execution_transaction_result

# Sealing only enumerates and reconciles here: aborting in flight needs an engine abort
# adapter, so this entry point never claims it and never returns a cleanup acknowledgement.
_ABORT_IN_FLIGHT = False


def barrier_fact_id(gateway_id: str, pool_id: str) -> str:
    """One durable barrier is keyed by gateway and pool, so its fact id carries both."""
    return f"{gateway_id}:{pool_id}"


def barrier_fact(snapshot: BarrierSnapshot) -> dict[str, object]:
    """The recorded fact: the sealed minimum is attested by its own proof digest."""
    return {
        "kind": "fact",
        "fact_id": barrier_fact_id(snapshot.gateway_id, snapshot.pool_id),
        "request_digest": snapshot.evidence_digest,
    }


class GatewayBarrierBoundary:
    def __init__(self, *, ingress: GatewayIngress, ledger: BarrierLedger) -> None:
        self._ingress = ingress
        self._ledger = ledger

    def seal_barrier(self, request: Any) -> dict[str, Any]:
        validate_coordination("seal_barrier", request)

        def seal() -> tuple[bool, int | None, dict[str, object]]:
            fence = self._fence(request["authority"], request["barrier"])
            if fence.evidence_digest != request["evidence_digest"]:
                raise InvalidExecutionRequestError(
                    "the carried proof does not describe this gateway's seal"
                )
            recorded = self._ledger.get_barrier(fence.gateway_id, fence.request.pool_id)
            if (
                recorded is not None
                and recorded.evidence_digest == fence.evidence_digest
                and recorded.min_fencing_token == fence.request.min_accepted_fencing_token
            ):
                # The original fact, without re-verifying the authority that has since changed.
                return True, recorded.version, barrier_fact(recorded)
            sealed = self._ingress.seal(fence.request, self._ledger)
            return False, sealed.version, barrier_fact(sealed)

        return execution_transaction_result("seal_barrier", self._ingress.gateway_id, seal)

    def _fence(self, authority: Any, observation: Any) -> IngressFence:
        """One enforced seal of this gateway, or a rejection that names what is wrong."""
        if observation["gateway_id"] != self._ingress.gateway_id:
            raise InvalidExecutionRequestError("the barrier belongs to another gateway")
        if observation["pool_id"] != authority["pool_id"]:
            raise InvalidExecutionRequestError("the barrier belongs to another pool")
        if observation["enforced_min_fencing_token"] != authority["fencing_token"]:
            raise InvalidExecutionRequestError("the barrier contradicts its own authority")
        return IngressFence(
            self._ingress.gateway_id,
            self._ingress.node_id,
            BarrierRequest(
                pool_id=authority["pool_id"],
                new_owner_scheduler_id=authority["owner_id"],
                min_accepted_fencing_token=authority["fencing_token"],
                abort_in_flight=_ABORT_IN_FLIGHT,
            ),
        )
