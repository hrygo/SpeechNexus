"""Close a successful execution only with the engine's own terminal fact.

The fixed private result is not stop evidence: it proves what was produced, not that
the engine stopped and released this execution's resources. This service turns an
adapter-observed terminal fact into ``record_stop``'s single success transition and
never invents one. Missing or unrelated evidence raises an explicit error so the
execution keeps holding capacity instead of being reported as succeeded.
"""

from __future__ import annotations

from speechnexus_gateway.domain.ports import ExecutionFactLedger
from speechnexus_gateway.domain.recovery import (
    TerminalStopEvidenceUnavailableError,
    engine_terminal_stop_evidence,
)
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionState,
    RecordStop,
    StopEvidenceConflictError,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
)
from speechnexus_gateway.domain.terminal import EngineTerminalResponse


class ExecutionCompletionService:
    """Owns the success transition; the Scheduler still owns budget release."""

    def __init__(self, *, ledger: ExecutionFactLedger) -> None:
        self._ledger = ledger

    def complete(
        self, accepted: SubmissionIntent, terminal: EngineTerminalResponse | None
    ) -> SubmissionIntent:
        """Return the recorded intent after one success transition or replay."""
        current = self._ledger.get(accepted.identity.submit_intent_id)
        if (
            current is None
            or current.identity != accepted.identity
            or current.request_digest != accepted.request_digest
            or current.engine_request_id != accepted.engine_request_id
        ):
            raise SubmissionOutcomeUnknownError("the original execution is unavailable")
        if current.execution_state.is_terminal:
            # Another accepted fact already closed this execution; never reverse it.
            return current
        evidence = engine_terminal_stop_evidence(current, terminal)
        if evidence is None:
            raise TerminalStopEvidenceUnavailableError(
                "no trusted engine terminal response covers this execution"
            )
        try:
            return self._ledger.record_stop(RecordStop(evidence, ExecutionState.SUCCEEDED))
        except ExecutionCleanupPendingError, StopEvidenceConflictError:
            closed = self._ledger.get(current.identity.submit_intent_id)
            if closed is not None and closed.execution_state.is_terminal:
                return closed
            raise
