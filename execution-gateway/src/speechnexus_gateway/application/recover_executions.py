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
    """What one pass did, counted from the durable facts it actually read and wrote."""

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

    def sweep(self, *, limit: int = 50) -> RecoverySweepReport:
        """Reconcile one bounded batch of this gateway's uncertain executions."""
        if type(limit) is not int or not 1 <= limit <= MAX_SWEEP_LIMIT:
            raise InvalidExecutionRequestError(
                f"a recovery sweep reads between 1 and {MAX_SWEEP_LIMIT} executions"
            )
        self._ingress.check_ownership()
        examined = observed = closed = still_unknown = 0
        for candidate in self._ledger.list_quarantined(
            gateway_id=self._ingress.gateway_id,
            host_node_id=self._ingress.node_id,
            limit=limit,
        ):
            examined += 1
            # One reconciliation rule for every path that adopts an engine fact.
            current = self._submissions.query(candidate.identity.submit_intent_id) or candidate
            if current.version != candidate.version:
                observed += 1
            if current.execution_state is not ExecutionState.UNKNOWN_QUARANTINED:
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
