"""One process-local periodic driver; all execution facts remain in the original ledger."""

from __future__ import annotations

import logging
import math
from threading import Event, Thread
from time import monotonic

from speechnexus_gateway.application.recover_executions import (
    MAX_SWEEP_LIMIT,
    ExecutionRecoveryService,
)

_LOGGER = logging.getLogger(__name__)


class RecoveryWorker:
    """Finish the current bounded call, join, then let the composition root close resources."""

    def __init__(
        self,
        service: ExecutionRecoveryService,
        *,
        interval_seconds: float,
        batch_size: int,
        pass_budget_seconds: float,
    ) -> None:
        if (
            not math.isfinite(interval_seconds)
            or interval_seconds <= 0
            or not math.isfinite(pass_budget_seconds)
            or pass_budget_seconds <= 0
            or type(batch_size) is not int
            or not 1 <= batch_size <= MAX_SWEEP_LIMIT
        ):
            raise ValueError("recovery requires bounded passes and intervals")
        self._service = service
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._budget = pass_budget_seconds
        self._stop = Event()
        self._thread = Thread(target=self._run, name="gateway-recovery", daemon=False)

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        if self._stop.is_set():
            raise RuntimeError("a closed recovery worker cannot start")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            # No timeout: returning while a writer is live would violate ownership.
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            deadline = monotonic() + self._budget

            def should_stop(deadline: float = deadline) -> bool:
                return self._stop.is_set() or monotonic() >= deadline

            try:
                self._service.sweep(
                    limit=self._batch_size,
                    should_stop=should_stop,
                )
            except Exception:
                # Preserve unknown and try the next page next time. Never log deployment
                # addresses, credentials, input/exception text or claim successful closure.
                _LOGGER.warning("gateway recovery pass failed; unresolved work remains held")
            if self._stop.wait(self._interval):
                break
