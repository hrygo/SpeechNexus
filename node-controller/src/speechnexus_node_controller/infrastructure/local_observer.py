"""One node-local observation loop; it never issues prepare/stop or owns budgets."""

from __future__ import annotations

import logging
import math
from threading import Event, Thread

from speechnexus_node_controller.application.runtime_control import (
    NodeRuntimeControlService,
    ObservationBatch,
)

logger = logging.getLogger(__name__)


class LocalObservationLoop:
    """Round-robin pending receipts, with one bounded inspection at a time."""

    def __init__(
        self,
        service: NodeRuntimeControlService,
        *,
        interval_seconds: float,
        batch_size: int,
    ) -> None:
        if (
            type(interval_seconds) not in (float, int)
            or not math.isfinite(interval_seconds)
            or not 0 < interval_seconds <= 3600
        ):
            raise ValueError("observation interval must be finite and within (0, 3600]")
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError("observation batch size must be within 1..500")
        self._service = service
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._stop = Event()
        self._thread: Thread | None = None
        self.last_batch: ObservationBatch | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the node observation loop is already started")
        self._stop.clear()
        self._thread = Thread(target=self._run, name="speechnexus-node-observer")
        self._thread.start()

    def close(self) -> None:
        """Finish an active bounded inspect before the owning lifespan closes its store."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self) -> None:
        cursor: str | None = None
        while not self._stop.is_set():
            try:
                batch = self._service.observe_pending(
                    after_intent_id=cursor,
                    limit=self._batch_size,
                    should_stop=self._stop.is_set,
                )
                cursor = batch.after_intent_id
                self.last_batch = batch
                if batch.failed:
                    logger.warning("node observations deferred: %d", batch.failed)
            except Exception:
                # Never log exception text, model paths, credentials or response bodies.
                # A failed pass leaves durable facts and its cursor untouched for retry.
                logger.error("node observation pass unavailable; retrying")
            self._stop.wait(self._interval_seconds)
