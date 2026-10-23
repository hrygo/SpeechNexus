"""Submission and stop application service for the execution gateway.

An intent is written before any engine request, a repeated intent never
dispatches again, and an ambiguous dispatch stays quarantined while it keeps
holding capacity until verified stop evidence arrives.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.ports import (
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
    """Owns the submit path and the capacity-releasing stop path."""

    def __init__(
        self,
        *,
        ledger: SubmissionLedger,
        dispatch: EngineDispatchPort,
        observation: EngineObservationPort,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._dispatch = dispatch
        self._observation = observation
        self._now = now or (lambda: datetime.now(UTC))

    def submit(self, request: SubmitRequest) -> SubmissionOutcome:
        """Persist, then dispatch once; replays and conflicts never dispatch."""
        identity = request.identity
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
        self._ledger.save(intent, expected_version=1)
        return SubmissionOutcome(intent=intent, idempotent_replay=False)

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
        if intent.execution_state is not ExecutionState.UNKNOWN_QUARANTINED:
            return intent
        version = intent.version
        reconciled = self._reconcile(intent)
        if reconciled.version != version:
            self._ledger.save(reconciled, expected_version=version)
        return reconciled

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
        intent.adopt_engine_observation(
            engine_request_id=observation.engine_request_id,
            engine_state=observation.state,
            at=self._now(),
            error_code=observation.error_code,
        )
        return intent
