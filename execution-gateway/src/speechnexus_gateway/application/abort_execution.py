"""Bounded engine abort: verified stop evidence or an honest quarantine.

A cancellation request, a transport timeout, a disconnect or an engine answer of
"not found" is never stop evidence. The engine adapter decides what it actually
observed; this service either records the two release confirmations it received or
leaves the execution quarantined and holding capacity.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.abort_intent import AbortExecutionRequest as AbortExecutionRequest
from speechnexus_gateway.domain.ports import (
    DurableAbortLedger,
    EngineAbortPort,
    ExecutionFactLedger,
)
from speechnexus_gateway.domain.recovery import ExecutionSnapshot, MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    RecordStop,
    StaleFencingTokenError,
    SubmissionStoreUnavailableError,
)


class ExecutionAbortService:
    """Owns abort facts; the Scheduler still owns budget and capacity release."""

    def __init__(
        self,
        *,
        ingress: GatewayIngress,
        ledger: ExecutionFactLedger,
        abort: EngineAbortPort,
        now: Callable[[], datetime] | None = None,
        durable_abort_enabled: bool = False,
    ) -> None:
        self._ingress = ingress
        self._ledger = ledger
        self._abort = abort
        self._now = now or (lambda: datetime.now(UTC))
        self._durable_abort_enabled = durable_abort_enabled

    def abort(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Return the recorded state after one bounded abort attempt, or None if unknown here."""
        return self._abort_request(request, durable=False)

    def abort_idempotent(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Accept an original abort receipt before claiming one external dispatch."""
        if not self._durable_abort_enabled or not isinstance(self._ledger, DurableAbortLedger):
            raise SubmissionStoreUnavailableError("durable abort authority is unavailable")
        return self._abort_request(request, durable=True)

    def _abort_request(
        self, request: AbortExecutionRequest, *, durable: bool
    ) -> ExecutionSnapshot | None:
        self._ingress.check_ownership()
        intent = self._ledger.get(request.submit_intent_id)
        if intent is None or (
            intent.identity.gateway_id,
            intent.identity.host_node_id,
        ) != (self._ingress.gateway_id, self._ingress.node_id):
            # Another node/gateway owns this execution, so this one has no abort to report.
            return None
        identity = intent.identity
        if (request.operation_id, request.attempt_id) != (
            identity.operation_id,
            identity.attempt_id,
        ):
            raise InvalidExecutionRequestError("abort belongs to another attempt of this execution")
        if request.fencing_token != identity.fencing_token:
            raise StaleFencingTokenError(
                "abort carries another fencing token than the recorded execution"
            )
        with self._ingress.abort_dispatch(identity):
            current = self._ledger.get(request.submit_intent_id)
            if current is None or current.identity != identity:
                raise InvalidExecutionRequestError("execution identity changed during abort")
            if durable:
                if not isinstance(self._ledger, DurableAbortLedger):
                    raise SubmissionStoreUnavailableError("durable abort authority is unavailable")
                self._ledger.receive_abort(request)
                claimed = self._ledger.claim_abort(request)
            else:
                claimed = None if current.execution_state.is_terminal else current
            if claimed is None:
                current = self._ledger.get(request.submit_intent_id)
                if current is None or current.identity != identity:
                    raise InvalidExecutionRequestError("execution identity changed during abort")
                return ExecutionSnapshot(current)
            outcome = self._abort.abort(
                claimed, reason=request.reason, wait_timeout_ms=request.wait_timeout_ms
            )
            evidence = outcome.evidence
            if outcome.verified_stop and evidence is not None:
                if evidence.submit_intent_id != identity.submit_intent_id:
                    raise InvalidExecutionRequestError("stop evidence belongs to another execution")
                stopped = self._ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED))
                return ExecutionSnapshot(stopped)
            if durable:
                return ExecutionSnapshot(claimed)
            observed_at = max(outcome.observed_at, claimed.updated_at)
            quarantined = self._ledger.mark_execution_unknown(
                MarkExecutionUnknown.for_intent(
                    claimed,
                    reason_code=ExecutionErrorCode.CANCELLATION_UNCONFIRMED,
                    at=observed_at,
                )
            )
            return ExecutionSnapshot(quarantined)
