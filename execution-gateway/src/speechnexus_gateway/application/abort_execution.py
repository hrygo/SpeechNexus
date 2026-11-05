"""Bounded engine abort: one recoverable attempt lease, verified stop, honest quarantine.

A cancellation request, a transport timeout, a disconnect or an engine answer of
"not found" is never stop evidence. The engine adapter decides what it actually
observed; this service either records the two release confirmations it received or
leaves the execution quarantined and holding capacity. The external attempt itself
is a durable lease: a crash after the claim, or after the engine answered but before
the local record committed, leaves an expired or undecided attempt that recovery
retries with the original identity instead of a claim nobody can ever release.
"""

from __future__ import annotations

from uuid import uuid4

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.abort_intent import (
    AbortDispatchOutcome,
    AbortDispatchResult,
    abort_dispatch_lease_ms,
)
from speechnexus_gateway.domain.abort_intent import (
    AbortExecutionRequest as AbortExecutionRequest,
)
from speechnexus_gateway.domain.ports import (
    AbortOutcome,
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
    SubmissionIdentity,
    SubmissionIntent,
    SubmissionStoreUnavailableError,
)


def _new_attempt_id() -> str:
    return "abort-" + uuid4().hex


def _origin_error(request: AbortExecutionRequest, identity: SubmissionIdentity) -> Exception | None:
    """Classify how the request stopped naming the authoritative execution."""
    if (request.operation_id, request.attempt_id) != (identity.operation_id, identity.attempt_id):
        return InvalidExecutionRequestError("abort belongs to another attempt of this execution")
    if request.fencing_token != identity.fencing_token:
        return StaleFencingTokenError(
            "abort carries another fencing token than the recorded execution"
        )
    if request.origin_known and (request.instance_id, request.instance_generation) != (
        identity.instance_id,
        identity.instance_generation,
    ):
        return InvalidExecutionRequestError(
            "abort origin disagrees with the authoritative instance generation"
        )
    return None


def _changed(
    current: SubmissionIntent | None,
    *,
    identity: SubmissionIdentity,
    request_digest: str,
    engine_request_id: str | None,
) -> bool:
    return (
        current is None
        or current.identity != identity
        or current.request_digest != request_digest
        or current.engine_request_id != engine_request_id
    )


class ExecutionAbortService:
    """Owns abort facts; the Scheduler still owns budget and capacity release."""

    def __init__(
        self,
        *,
        ingress: GatewayIngress,
        ledger: ExecutionFactLedger,
        abort: EngineAbortPort,
        durable_abort_enabled: bool = False,
    ) -> None:
        self._ingress = ingress
        self._ledger = ledger
        self._abort = abort
        self._durable_abort_enabled = durable_abort_enabled

    def abort(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Return the recorded state after one bounded abort attempt, or None if unknown here."""
        return self._abort_request(request)

    def abort_idempotent(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Accept an original abort receipt before taking the next bounded attempt."""
        return self._abort_request(request)

    def recover_abort(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Resume the original receipt; a stale authority is refused with durable evidence."""
        return self._abort_request(request, recovering=True)

    def _abort_request(
        self, request: AbortExecutionRequest, *, recovering: bool = False
    ) -> ExecutionSnapshot | None:
        ledger = self._durable_ledger()
        self._ingress.check_ownership()
        intent = self._ledger.get(request.submit_intent_id)
        if intent is None or (
            intent.identity.gateway_id,
            intent.identity.host_node_id,
        ) != (self._ingress.gateway_id, self._ingress.node_id):
            # Another node/gateway owns this execution, so this one has no abort to report.
            return None
        identity = intent.identity
        drift = _origin_error(request, identity)
        if drift is not None:
            if not recovering:
                raise drift
            return self._refuse_stale_origin(request, intent)
        # Receive in its own committed transaction before waiting for a local sender.
        # Any rollback or unknown commit result must propagate without a claim or send.
        ledger.receive_abort(request)
        with self._ingress.abort_dispatch(identity):
            self._require_unchanged(self._ledger.get(request.submit_intent_id), intent)
            claim = ledger.claim_abort(
                request,
                attempt_id=_new_attempt_id(),
                lease_ms=abort_dispatch_lease_ms(request.wait_timeout_ms),
            )
            if claim is None:
                # A live attempt lease, a decided answer or a terminal execution:
                # this caller has no external authority and sends nothing.
                return ExecutionSnapshot(
                    self._require_unchanged(self._ledger.get(request.submit_intent_id), intent)
                )
            claimed = self._require_unchanged(claim.intent, intent)
            outcome = self._abort.abort(
                claimed, reason=request.reason, wait_timeout_ms=request.wait_timeout_ms
            )
            snapshot = self._record_outcome(claimed, outcome)
            # The stop fact is committed first. A crash between the two leaves the
            # attempt undecided, and the retry replays the same evidence identity.
            ledger.record_abort_dispatch(
                AbortDispatchResult(
                    submit_intent_id=request.submit_intent_id,
                    attempt_id=claim.attempt_id,
                    outcome=(
                        AbortDispatchOutcome.ACKNOWLEDGED
                        if outcome.acknowledgement.decided
                        else AbortDispatchOutcome.UNCONFIRMED
                    ),
                    observed_at=outcome.observed_at,
                )
            )
            return snapshot

    def _refuse_stale_origin(
        self, request: AbortExecutionRequest, intent: SubmissionIntent
    ) -> ExecutionSnapshot | None:
        """Keep the receipt, refuse the send and leave a durable refusal behind."""
        identity = intent.identity
        with self._ingress.abort_dispatch(identity):
            current = self._require_unchanged(self._ledger.get(request.submit_intent_id), intent)
            self._durable_ledger().reject_abort(request, attempt_id=_new_attempt_id())
            return ExecutionSnapshot(current)

    def _durable_ledger(self) -> DurableAbortLedger:
        ledger = self._ledger
        if not self._durable_abort_enabled or not isinstance(ledger, DurableAbortLedger):
            raise SubmissionStoreUnavailableError("durable abort authority is unavailable")
        return ledger

    def _require_unchanged(
        self, current: SubmissionIntent | None, intent: SubmissionIntent
    ) -> SubmissionIntent:
        if _changed(
            current,
            identity=intent.identity,
            request_digest=intent.request_digest,
            engine_request_id=intent.engine_request_id,
        ):
            raise InvalidExecutionRequestError("execution identity changed during abort")
        assert current is not None
        return current

    def _record_outcome(self, intent: SubmissionIntent, outcome: AbortOutcome) -> ExecutionSnapshot:
        evidence = outcome.evidence
        if outcome.verified_stop and evidence is not None:
            if evidence.submit_intent_id != intent.identity.submit_intent_id:
                raise InvalidExecutionRequestError("stop evidence belongs to another execution")
            stopped = self._ledger.record_stop(RecordStop(evidence, ExecutionState.ABORTED))
            return ExecutionSnapshot(stopped)
        return ExecutionSnapshot(intent)
