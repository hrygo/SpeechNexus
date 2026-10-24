"""Bounded engine abort: verified stop evidence or an honest quarantine.

A cancellation request, a transport timeout, a disconnect or an engine answer of
"not found" is never stop evidence. The engine adapter decides what it actually
observed; this service either records the two release confirmations it received or
leaves the execution quarantined and holding capacity.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.ports import EngineAbortPort, ExecutionAbortLedger
from speechnexus_gateway.domain.recovery import ExecutionSnapshot, MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    RecordStop,
    StaleFencingTokenError,
)

ABORT_REASONS = frozenset(
    {
        "client_cancelled",
        "deadline_exceeded",
        "quota_exceeded",
        "resource_force_disabled",
        "gateway_takeover_reconcile",
        "stream_disconnected",
    }
)

_MIN_WAIT_MS = 100
_MAX_WAIT_MS = 60000


@dataclass(frozen=True, slots=True)
class AbortExecutionRequest:
    """One abort of a recorded execution by its own identity and fencing token."""

    submit_intent_id: str
    operation_id: str
    attempt_id: str
    fencing_token: int
    reason: str
    wait_timeout_ms: int

    def __post_init__(self) -> None:
        if self.reason not in ABORT_REASONS:
            raise InvalidExecutionRequestError("unknown abort reason")
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise InvalidExecutionRequestError("abort requires a positive fencing token")
        if (
            type(self.wait_timeout_ms) is not int
            or not _MIN_WAIT_MS <= self.wait_timeout_ms <= _MAX_WAIT_MS
        ):
            raise InvalidExecutionRequestError("abort wait must be within the contract bounds")


class ExecutionAbortService:
    """Owns abort facts; the Scheduler still owns budget and capacity release."""

    def __init__(
        self,
        *,
        ingress: GatewayIngress,
        ledger: ExecutionAbortLedger,
        abort: EngineAbortPort,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ingress = ingress
        self._ledger = ledger
        self._abort = abort
        self._now = now or (lambda: datetime.now(UTC))

    def abort(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Return the recorded state after one bounded abort attempt, or None if unknown here."""
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
        if intent.execution_state.is_terminal:
            # Already closed: report the recorded original identity without touching the engine.
            return ExecutionSnapshot(intent)

        outcome = self._abort.abort(
            intent, reason=request.reason, wait_timeout_ms=request.wait_timeout_ms
        )
        evidence = outcome.evidence
        if outcome.verified_stop and evidence is not None:
            if evidence.submit_intent_id != identity.submit_intent_id:
                raise InvalidExecutionRequestError("stop evidence belongs to another execution")
            stopped = self._ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED))
            return ExecutionSnapshot(stopped)

        # Nothing was verified, so the execution stays unknown and keeps its capacity.
        # The recorded instant never precedes the fact it replaces.
        observed_at = max(outcome.observed_at, intent.updated_at)
        quarantined = self._ledger.mark_execution_unknown(
            MarkExecutionUnknown.for_intent(
                intent,
                reason_code=ExecutionErrorCode.CANCELLATION_UNCONFIRMED,
                at=observed_at,
            )
        )
        return ExecutionSnapshot(quarantined)
