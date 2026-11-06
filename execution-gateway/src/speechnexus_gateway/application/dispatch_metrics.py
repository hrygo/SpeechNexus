"""Process-local counts of the durable abort dispatch outcomes.

The gateway serves no metrics scrape endpoint: ``/healthz`` and ``/readyz`` are the
frozen operational surface. Counts therefore leave the process exactly like every
other operational fact: as one structured log line with bounded labels (the outcome
names, never an execution, attempt or engine identity), next to the durable
``dispatch_outcome`` / ``dispatch_attempts`` columns the read-only query role may
read. This module owns only the aggregate; it adds no exporter, route or schema.
"""

from threading import Lock

from speechnexus_gateway.domain.abort_intent import AbortDispatchOutcome


class DispatchOutcomeCounters:
    """Monotonic per-process counts; a restart resets them, the ledger does not."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts = {outcome.value: 0 for outcome in AbortDispatchOutcome}
        self._logged: dict[str, int] = dict(self._counts)

    def record(self, outcome: AbortDispatchOutcome) -> None:
        if not isinstance(outcome, AbortDispatchOutcome):
            raise ValueError("dispatch counters require a known outcome")
        with self._lock:
            self._counts[outcome.value] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def take_change(self) -> dict[str, int] | None:
        """Return the current counts when they moved since the last emission."""
        with self._lock:
            if self._counts == self._logged:
                return None
            self._logged = dict(self._counts)
            return dict(self._counts)
