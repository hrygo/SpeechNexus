"""Standalone bounded retention/reconciliation worker with explicit identity."""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Callable
from threading import Event

from speechnexus_backend.asset_runtime import AssetRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.assets import AssetService
from speechnexus_backend.speech.domain.assets import AssetError

_LOG = logging.getLogger(__name__)


class _StopRequested(BaseException):
    """Unwind short transactions/readers without acquiring a signal-handler lock."""


def run_maintenance(
    service: AssetService,
    stop: Event,
    *,
    interval_seconds: float,
    batch_size: int,
    once: bool = False,
    on_error: Callable[[], None] | None = None,
) -> None:
    if not 0 < interval_seconds <= 300 or not 1 <= batch_size <= 100:
        raise ValueError("Asset maintenance must be bounded")
    while not stop.is_set():
        try:
            service.maintain(batch_size)
        except AssetError:
            # Deliberately exclude SDK/SQL diagnostics and any request content.
            if on_error is not None:
                on_error()
            else:
                _LOG.warning("Asset maintenance unavailable; persistent originals retained")
            if once:
                raise
        if once or stop.wait(interval_seconds):
            return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile original assets and replay retention ledgers"
    )
    parser.add_argument("--once", action="store_true", help="Run one bounded pass, then exit")
    options = parser.parse_args()
    settings = Settings()
    if settings.process_role != "job-worker":
        parser.error("Set SPEECHNEXUS_PROCESS_ROLE=job-worker")
    if not {"node_id", "instance_id"} <= settings.model_fields_set:
        parser.error("Set explicit SPEECHNEXUS_NODE_ID and SPEECHNEXUS_INSTANCE_ID")
    stop = Event()

    def request_stop(signum: int, frame: object) -> None:
        # Event.set() can deadlock when a signal interrupts Event.wait() while
        # it holds its own non-reentrant lock. Exceptions unwind our owned
        # contexts; an interrupted delete/commit retains its durable identity.
        raise _StopRequested

    runtime = AssetRuntime(settings)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        runtime.open()
        run_maintenance(
            runtime.service,
            stop,
            interval_seconds=settings.asset_maintenance_interval_seconds,
            batch_size=settings.asset_maintenance_batch_size,
            once=options.once,
        )
    except _StopRequested:
        pass
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        runtime.close()


if __name__ == "__main__":
    main()
