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

import logging
from uuid import uuid4

from speechnexus_gateway.application.dispatch_metrics import DispatchOutcomeCounters
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.abort_intent import (
    MAX_ABORT_DISPATCH_ATTEMPTS,
    AbortClaimRefusal,
    AbortDispatchOutcome,
    AbortDispatchResult,
    abort_dispatch_lease_ms,
    abort_retry_delay_ms,
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

logger = logging.getLogger(__name__)


def _new_attempt_id() -> str:
    return "abort-" + uuid4().hex


def _outcome_for(*, decided: bool, attempts: int) -> AbortDispatchOutcome:
    """Decided answers end the receipt; spent attempts give it up instead of looping."""
    if decided:
        return AbortDispatchOutcome.ACKNOWLEDGED
    if attempts >= MAX_ABORT_DISPATCH_ATTEMPTS:
        return AbortDispatchOutcome.EXHAUSTED
    return AbortDispatchOutcome.UNCONFIRMED


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
    """Owns abort facts; the Scheduler still owns budget and capacity release.

    Every entry point performs the same bounded attempt - receive, claim one lease,
    call the adapter at most once, record what it learned - so a replay, a fresh HTTP
    request and a recovery sweep all resume the original receipt instead of creating a
    second execution. The process-local counters aggregate the outcomes for operators;
    the durable receipt remains the authoritative fact.
    """

    def __init__(
        self,
        *,
        ingress: GatewayIngress,
        ledger: ExecutionFactLedger,
        abort: EngineAbortPort,
        durable_abort_enabled: bool = False,
        counters: DispatchOutcomeCounters | None = None,
    ) -> None:
        self._ingress = ingress
        self._ledger = ledger
        self._abort = abort
        self._durable_abort_enabled = durable_abort_enabled
        self._counters = DispatchOutcomeCounters() if counters is None else counters

    @property
    def dispatch_outcome_counts(self) -> DispatchOutcomeCounters:
        return self._counters

    def abort(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Return the recorded state after one bounded abort attempt, or None if unknown here."""
        return self._abort_request(request)

    def abort_idempotent(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Accept an original abort receipt before taking the next bounded attempt."""
        return self._abort_request(request)

    def recover_abort(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
        """Resume the original receipt recorded for this execution."""
        return self._abort_request(request)

    def _abort_request(self, request: AbortExecutionRequest) -> ExecutionSnapshot | None:
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
            # The recorded identity is immutable: a disagreement is corruption, not a
            # state to persist. Fail loudly and send nothing against another origin.
            raise drift
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
            if isinstance(claim, AbortClaimRefusal):
                if claim is AbortClaimRefusal.EXHAUSTED:
                    # A crash inside the final attempt left no outcome to record:
                    # this claim wrote the give-up, so it is the one counted refusal.
                    self._counters.record(AbortDispatchOutcome.EXHAUSTED)
                # Live lease, decided answer or terminal execution: this caller has no
                # external authority and sends nothing.
                return ExecutionSnapshot(
                    self._require_unchanged(self._ledger.get(request.submit_intent_id), intent)
                )
            claimed = self._require_unchanged(claim.intent, intent)
            outcome = self._abort.abort(
                claimed, reason=request.reason, wait_timeout_ms=request.wait_timeout_ms
            )
            snapshot = self._record_outcome(claimed, outcome)
            dispatch_outcome = _outcome_for(
                decided=outcome.acknowledgement.decided, attempts=claim.attempts
            )
            self._counters.record(dispatch_outcome)
            if dispatch_outcome is AbortDispatchOutcome.EXHAUSTED:
                # Visible evidence for operators: the receipt keeps the bounded
                # attempt count and the execution keeps holding its capacity.
                logger.warning(
                    "abort attempts exhausted without a decidable engine answer: %s",
                    request.submit_intent_id,
                )
            # The stop fact is committed first. A crash between the two leaves the
            # attempt undecided, and the retry replays the same evidence identity.
            ledger.record_abort_dispatch(
                AbortDispatchResult(
                    submit_intent_id=request.submit_intent_id,
                    attempt_id=claim.attempt_id,
                    outcome=dispatch_outcome,
                    observed_at=outcome.observed_at,
                    retry_delay_ms=(
                        None if dispatch_outcome.decided else abort_retry_delay_ms(claim.attempts)
                    ),
                )
            )
            return snapshot

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
