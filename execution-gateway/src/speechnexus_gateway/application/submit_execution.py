"""Submission and stop application service for the execution gateway.

An intent is written before any engine request, a repeated intent never
dispatches again, and an ambiguous dispatch stays quarantined while it keeps
holding capacity until verified stop evidence arrives.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.observe_execution import check_gateway_scope
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.ports import (
    CommittedExecutionStarter,
    EngineDispatchPort,
    EngineObservationPort,
    SubmissionLedger,
)
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    EngineObservedState,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    RecordStop,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmitIntentConflictError,
    SubmitRequest,
    VerifiedStopEvidence,
)


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    """Durable intent plus whether the caller replayed an existing submission."""

    intent: SubmissionIntent
    idempotent_replay: bool


class SubmitExecutionService:
    """Owns submit and stop facts; Scheduler owns capacity release."""

    def __init__(
        self,
        *,
        ledger: SubmissionLedger,
        ingress: GatewayIngress,
        dispatch: EngineDispatchPort,
        observation: EngineObservationPort,
        committed_execution: CommittedExecutionStarter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._ingress = ingress
        self._dispatch = dispatch
        self._observation = observation
        self._committed_execution = committed_execution
        self._now = now or (lambda: datetime.now(UTC))

    def submit(self, request: SubmitRequest) -> SubmissionOutcome:
        """Persist, then dispatch once; replays and conflicts never dispatch."""
        identity = request.identity
        check_gateway_scope(self._ingress, identity)
        digest = command_digest(request.command)
        if digest != identity.command_digest:
            raise InvalidExecutionRequestError(
                "command_digest does not match the canonical command"
            )

        existing = self._ledger.get(identity.submit_intent_id)
        if existing is not None:
            if existing.request_digest != request.request_digest:
                raise SubmitIntentConflictError(
                    "submit intent already exists with another effective request"
                )
            # query owns the conditional save when a delayed executing fact arrives.
            return SubmissionOutcome(
                intent=self.query(identity.submit_intent_id) or existing, idempotent_replay=True
            )

        with self._ingress.dispatch(identity):
            # Another caller may have persisted this identity while we waited for entry.
            existing = self._ledger.get(identity.submit_intent_id)
            if existing is not None:
                if existing.request_digest != request.request_digest:
                    raise SubmitIntentConflictError("submit intent has another effective request")
                return SubmissionOutcome(intent=existing, idempotent_replay=True)
            try:
                outcome = self._submit_new(request)
            except BaseException:
                self._discard_preparation(identity.submit_intent_id)
                raise

        # A long file task must not retain the ingress send lock. Only this
        # invocation's newly committed acceptance may reach the start callback.
        if self._committed_execution is not None:
            if outcome.intent.execution_state is ExecutionState.EXECUTING:
                try:
                    self._committed_execution.start(outcome.intent)
                except Exception:
                    self._discard_preparation(identity.submit_intent_id)
                    raise SubmissionOutcomeUnknownError(
                        "the accepted execution must be queried by its original identity"
                    ) from None
            else:
                self._discard_preparation(identity.submit_intent_id)
        return outcome

    def _discard_preparation(self, submit_intent_id: str) -> None:
        if self._committed_execution is not None:
            # Preparation cleanup cannot erase a commit ambiguity. Running
            # execution/resource cleanup still requires its original facts.
            with suppress(Exception):
                self._committed_execution.discard(submit_intent_id)

    def _submit_new(self, request: SubmitRequest) -> SubmissionOutcome:
        identity = request.identity
        at = self._now()
        intent = SubmissionIntent(
            identity=identity,
            recorded_at=at,
            updated_at=at,
            request_digest=request.request_digest,
            output_stream_id=request.output_stream_id,
        )
        self._ledger.add(intent, request_document=request.request_document)
        outcome = self._dispatch.dispatch(intent)
        if outcome.result is DispatchResult.ACKNOWLEDGED:
            if outcome.engine_request_id is None:
                raise ValueError("acknowledged dispatch carries an engine request id")
            intent.mark_dispatched(outcome.engine_request_id, at=outcome.observed_at)
        elif outcome.result is DispatchResult.NOT_SENT:
            intent.mark_not_dispatched_failure(
                ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, at=outcome.observed_at
            )
        else:
            intent.mark_dispatch_unknown(at=outcome.observed_at)
        saved = self._ledger.record_execution_observation(
            RecordExecutionObservation.from_intent(intent, expected_version=1)
        )
        return SubmissionOutcome(intent=saved, idempotent_replay=False)

    def record_stop(
        self,
        evidence: VerifiedStopEvidence,
        *,
        terminal_state: ExecutionState = ExecutionState.ABORTED,
        error_code: ExecutionErrorCode | None = None,
    ) -> SubmissionIntent:
        """Persist stop and release facts atomically; Scheduler owns budget release."""
        return self._ledger.record_stop(RecordStop(evidence, terminal_state, error_code))

    def query(self, submit_intent_id: str) -> SubmissionIntent | None:
        """Read an intent, reconciling a quarantined one against the engine."""
        intent = self._ledger.get(submit_intent_id)
        if intent is None:
            return None
        check_gateway_scope(self._ingress, intent.identity)
        if intent.execution_state is not ExecutionState.UNKNOWN_QUARANTINED:
            return intent
        version = intent.version
        reconciled = self._reconcile(intent)
        if reconciled.version != version:
            return self._ledger.record_execution_observation(
                RecordExecutionObservation.from_intent(reconciled, expected_version=version)
            )
        return intent

    def _reconcile(self, intent: SubmissionIntent) -> SubmissionIntent:
        """Adopt an engine fact for the original identity; never dispatch again."""
        if intent.execution_state is not ExecutionState.UNKNOWN_QUARANTINED:
            return intent
        observation = self._observation.observe(intent)
        if observation is None:
            return intent
        if observation.state is not EngineObservedState.EXECUTING:
            # A status alone does not attest resource release. Only record_stop can
            # atomically persist verified terminal evidence and close the execution.
            return intent
        if intent.dispatched_at is None and observation.dispatched_at is None:
            return (
                intent  # An observed executing status does not reveal the original dispatch time.
            )
        intent.adopt_engine_observation(
            engine_request_id=observation.engine_request_id,
            engine_state=observation.state,
            at=self._now(),
            error_code=observation.error_code,
            dispatched_at=observation.dispatched_at,
        )
        return intent
