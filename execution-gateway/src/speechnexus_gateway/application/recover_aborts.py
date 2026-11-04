"""Bounded durable scheduling of original cancellation receipts."""

import logging
from collections.abc import Callable

from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.recover_executions import MAX_SWEEP_LIMIT
from speechnexus_gateway.domain.ports import AbortRecoveryQueue

_LOGGER = logging.getLogger(__name__)


class AbortRecoveryService:
    def __init__(
        self, *, ingress: GatewayIngress, ledger: AbortRecoveryQueue, abort: ExecutionAbortService
    ) -> None:
        self._ingress = ingress
        self._ledger = ledger
        self._abort = abort

    def sweep(self, *, limit: int, should_stop: Callable[[], bool] = lambda: False) -> int:
        if type(limit) is not int or not 1 <= limit <= MAX_SWEEP_LIMIT:
            raise ValueError("invalid abort recovery batch size")
        with self._ingress.recovery_hold():
            if should_stop():
                return 0
            count = 0
            while count < limit:
                if should_stop():
                    break
                requests = self._ledger.take_due_aborts(
                    gateway_id=self._ingress.gateway_id,
                    host_node_id=self._ingress.node_id,
                    limit=1,
                )
                if not requests:
                    break
                try:
                    self._abort.recover_abort(requests[0])
                except Exception:
                    _LOGGER.warning("abort recovery failed; original intent remains held")
                count += 1
            return count
