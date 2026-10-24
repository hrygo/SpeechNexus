"""One bounded drain of an engine instance generation, without inventing a stop.

The gateway owns the drain report and never the budget: it reads what this gateway still
has unfinished for that generation, asks the abort service for a bounded stop when one is
configured, and then re-reads. ``drained`` and ``safe_to_stop_instance`` are only reported
when that fresh read is empty; an unconfirmed stop leaves the generation quarantined and
still holding capacity.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from speechnexus_gateway.application.abort_execution import (
    AbortExecutionRequest,
    ExecutionAbortService,
)
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.instance_drain import (
    DrainInventory,
    DrainRequest,
    DrainSnapshot,
    InstanceIdentity,
)
from speechnexus_gateway.domain.ports import InstanceDrainLedger
from speechnexus_gateway.domain.submission import (
    InstanceGenerationMismatchError,
    StaleFencingTokenError,
    SubmissionIntent,
)

#: A drain stops work for the same reason a takeover reconcile does. The frozen abort
#: reasons are shared with ``abortExecution`` and are not extended by this route.
DRAIN_ABORT_REASON = "gateway_takeover_reconcile"

_MIN_ABORT_WAIT_MS = 100
_MAX_ABORT_WAIT_MS = 60000


class InstanceDrainService:
    """Reports one instance generation's drain state; the Scheduler still owns capacity.

    ``abort`` is optional because an engine abort adapter is not part of every deployment.
    Without it a drain only reports: nothing is written, nothing is stopped, and a
    generation with unfinished work is never called safe to stop. Only work with a recorded
    engine request identity is offered to that adapter, because an abort needs an identity
    the engine already received.
    """

    def __init__(
        self,
        *,
        ingress: GatewayIngress,
        ledger: InstanceDrainLedger,
        abort: ExecutionAbortService | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._ingress = ingress
        self._ledger = ledger
        self._abort = abort
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic

    def drain(self, request: DrainRequest) -> DrainSnapshot:
        """Return the freshness-checked drain state of one instance generation."""
        self._ingress.check_ownership()
        identity = self._verify_authority(request)
        inventory = self._ledger.read_drain_inventory(identity)
        abort = self._abort
        if abort is not None and inventory.holds_unfinished_work:
            inventory = self._stop_unfinished_work(abort, request, identity, inventory)
        return DrainSnapshot(request=request, inventory=inventory, observed_at=self._now())

    def _verify_authority(self, request: DrainRequest) -> InstanceIdentity:
        """Reject a drain that names another node/pool/gateway's generation or a stale token."""
        if request.host_node_id != self._ingress.node_id:
            raise InstanceGenerationMismatchError("the drain names another host node")
        identity = self._ledger.read_instance_identity(
            request.instance_id, request.instance_generation
        )
        if identity is None or (
            identity.host_node_id,
            identity.pool_id,
            identity.gateway_id,
        ) != (request.host_node_id, request.pool_id, self._ingress.gateway_id):
            raise InstanceGenerationMismatchError(
                "the drain does not match this gateway's recorded instance generation"
            )
        if request.fencing_token < self._enforced_minimum(request.pool_id):
            raise StaleFencingTokenError(
                "the drain carries a fencing token this gateway already rejects"
            )
        return identity

    def _enforced_minimum(self, pool_id: str) -> int:
        """The strongest minimum this gateway enforces: recorded barrier plus local floor."""
        barrier = self._ledger.get_barrier(self._ingress.gateway_id, pool_id)
        sealed = 0 if barrier is None else barrier.min_fencing_token
        return max(sealed, self._ingress.enforced_minimum(pool_id))

    def _stop_unfinished_work(
        self,
        abort: ExecutionAbortService,
        request: DrainRequest,
        identity: InstanceIdentity,
        inventory: DrainInventory,
    ) -> DrainInventory:
        """Boundedly stop what is left, then re-read instead of trusting the attempt."""
        deadline = self._monotonic() + request.max_drain_wait_ms / 1000
        for submit_intent_id in inventory.unfinished_intent_ids:
            remaining_ms = int((deadline - self._monotonic()) * 1000)
            if remaining_ms < _MIN_ABORT_WAIT_MS:
                break
            intent = self._ledger.get(submit_intent_id)
            if intent is None or intent.execution_state.is_terminal:
                continue
            if intent.dispatched_at is None:
                # No engine request identity was ever issued, so an abort has nothing to
                # stop. The intent keeps holding capacity; its release belongs to the
                # Scheduler and the recorded dispatch outcome, not to a drain.
                continue
            self._stop(abort, intent, wait_timeout_ms=min(remaining_ms, _MAX_ABORT_WAIT_MS))
        return self._ledger.read_drain_inventory(identity)

    def _stop(
        self, abort: ExecutionAbortService, intent: SubmissionIntent, *, wait_timeout_ms: int
    ) -> None:
        """Delegate one stop to the abort service so both routes share the same rules."""
        identity = intent.identity
        abort.abort(
            AbortExecutionRequest(
                submit_intent_id=identity.submit_intent_id,
                operation_id=identity.operation_id,
                attempt_id=identity.attempt_id,
                fencing_token=identity.fencing_token,
                reason=DRAIN_ABORT_REASON,
                wait_timeout_ms=wait_timeout_ms,
            )
        )
