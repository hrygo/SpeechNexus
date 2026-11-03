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
from speechnexus_gateway.domain.recovery import ExecutionSnapshot
from speechnexus_gateway.domain.submission import (
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
        return self._abort_request(request)

    def abort_idempotent(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Accept an original abort receipt before claiming one external dispatch."""
        return self._abort_request(request)

    def _abort_request(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        if not self._durable_abort_enabled or not isinstance(self._ledger, DurableAbortLedger):
            raise SubmissionStoreUnavailableError("durable abort authority is unavailable")
        ledger = self._ledger
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
        # Receive in its own committed transaction before waiting for a local sender.
        # Any rollback or unknown commit result must propagate without a claim or send.
        ledger.receive_abort(request)
        with self._ingress.abort_dispatch(identity):
            current = self._ledger.get(request.submit_intent_id)
            if (
                current is None
                or current.identity != identity
                or current.request_digest != intent.request_digest
            ):
                raise InvalidExecutionRequestError("execution identity changed during abort")
            claimed = ledger.claim_abort(request)
            if claimed is None:
                current = self._ledger.get(request.submit_intent_id)
                if (
                    current is None
                    or current.identity != identity
                    or current.request_digest != intent.request_digest
                ):
                    raise InvalidExecutionRequestError("execution identity changed during abort")
                return ExecutionSnapshot(current)
            if claimed.identity != identity or claimed.request_digest != intent.request_digest:
                raise InvalidExecutionRequestError("execution identity changed during abort claim")
            outcome = self._abort.abort(
                claimed, reason=request.reason, wait_timeout_ms=request.wait_timeout_ms
            )
            evidence = outcome.evidence
            if outcome.verified_stop and evidence is not None:
                if evidence.submit_intent_id != identity.submit_intent_id:
                    raise InvalidExecutionRequestError("stop evidence belongs to another execution")
                stopped = self._ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED))
                return ExecutionSnapshot(stopped)
            return ExecutionSnapshot(claimed)
