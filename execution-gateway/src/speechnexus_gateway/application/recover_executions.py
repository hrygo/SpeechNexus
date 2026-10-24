"""Bounded recovery of uncertain executions; capacity stays held until proven released.

One pass enumerates the executions this gateway and node already hold as unknown, adopts
whatever the engine reports for the identity the gateway issued, and closes an execution
only when a trusted producer reports that the whole instance generation terminated and its
resources were released. Nothing else closes it: a missing answer, a status, a timeout or
a transport failure keeps the execution quarantined and holding its capacity. The sweep
never writes a budget, never dispatches, and never fabricates stop evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.ports import (
    ExecutionSweepLedger,
    InstanceTerminationEvidencePort,
)
from speechnexus_gateway.domain.recovery import whole_instance_stop_evidence
from speechnexus_gateway.domain.submission import (
    ExecutionState,
    InvalidExecutionRequestError,
    RecordStop,
)

#: The frozen upper bound of one pass: a sweep is a bounded read, not a full table walk.
MAX_SWEEP_LIMIT = 500


@dataclass(frozen=True, slots=True)
class RecoverySweepReport:
    """Exclusive outcomes: left unknown state, verified closure, or still unknown.

    A version change alone is not an observation: generated usage can change the
    version while the execution remains unknown. A closure includes an accepted
    identical stop replay after a race. Every examined row counts once.
    """

    examined: int
    observed: int
    closed: int
    still_unknown: int

    def __post_init__(self) -> None:
        for name in ("examined", "observed", "closed", "still_unknown"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError("a recovery report counts nonnegative whole numbers")
        if self.observed + self.closed + self.still_unknown != self.examined:
            raise ValueError("every examined execution is observed, closed or still unknown")


class ExecutionRecoveryService:
    """Owns the recovery pass; the Scheduler still owns every budget decision."""

    def __init__(
        self,
        *,
        ingress: GatewayIngress,
        ledger: ExecutionSweepLedger,
        submissions: SubmitExecutionService,
        terminations: InstanceTerminationEvidencePort,
    ) -> None:
        self._ingress = ingress
        self._ledger = ledger
        self._submissions = submissions
        self._terminations = terminations
        self._after_submit_intent_id: str | None = None
        self._scan_lock = Lock()

    def sweep(self, *, limit: int = 50) -> RecoverySweepReport:
        """Visit one ID page; an empty page wraps the next pass to the beginning.

        Retain this service across passes. The cursor is process-local scheduling
        state, never an execution fact or release permission. Restart begins at the
        first page; concurrent passes are rejected without queuing.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_SWEEP_LIMIT:
            raise InvalidExecutionRequestError(
                f"a recovery sweep reads between 1 and {MAX_SWEEP_LIMIT} executions"
            )
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("a recovery sweep is already running")
        try:
            return self._sweep(limit)
        finally:
            self._scan_lock.release()

    def _sweep(self, limit: int) -> RecoverySweepReport:
        self._ingress.check_ownership()
        examined = observed = closed = still_unknown = 0
        candidates = self._ledger.list_quarantined(
            gateway_id=self._ingress.gateway_id,
            host_node_id=self._ingress.node_id,
            limit=limit,
            after_submit_intent_id=self._after_submit_intent_id,
        )
        if not candidates:
            self._after_submit_intent_id = None
        for candidate in candidates:
            # A failed observation/stop must not pin every later ID forever.
            # Advance only local visit order, never updated_at or durable state.
            self._after_submit_intent_id = candidate.identity.submit_intent_id
            examined += 1
            # One reconciliation rule for every path that adopts an engine fact.
            current = self._submissions.query(candidate.identity.submit_intent_id) or candidate
            if current.execution_state is not ExecutionState.UNKNOWN_QUARANTINED:
                observed += 1
                continue
            stop = whole_instance_stop_evidence(
                current, self._terminations.termination_evidence(current)
            )
            if stop is None:
                still_unknown += 1
                continue
            self._ledger.record_stop(RecordStop(stop, ExecutionState.ABORTED))
            closed += 1
        return RecoverySweepReport(
            examined=examined, observed=observed, closed=closed, still_unknown=still_unknown
        )
