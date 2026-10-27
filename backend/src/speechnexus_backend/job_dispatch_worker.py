"""Standalone public Job dispatcher process with explicit identity and stop."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from collections.abc import Callable

from speechnexus_backend.job_dispatch_runtime import (
    DispatchDependenciesUnavailableError,
    JobDispatchRuntime,
    JobDispatchSettings,
    build_dispatch_dependencies,
)
from speechnexus_backend.speech.application.job_dispatcher import (
    DispatchPlanner,
    JobDispatcher,
    ResultPublisher,
)
from speechnexus_backend.speech.domain.job_dispatch import DispatchBatch

BATCH_LOG_FIELDS = (
    "event",
    "claimed",
    "succeeded",
    "failed",
    "unknown",
    "skipped",
    "deferred",
    "lease_reclaimed",
    "lease_lost",
)


def batch_log(batch: DispatchBatch) -> dict[str, int | str]:
    """Stable counters only: no request content, endpoint or credential data."""

    return {
        "event": "job_dispatch_batch",
        "claimed": batch.claimed,
        "succeeded": batch.succeeded,
        "failed": batch.failed,
        "unknown": batch.unknown,
        "skipped": batch.skipped,
        "deferred": batch.deferred,
        "lease_reclaimed": batch.lease_reclaimed,
        "lease_lost": batch.lease_lost,
    }


async def run_dispatch_worker(
    dispatcher: JobDispatcher,
    worker_id: str,
    stop: asyncio.Event,
    *,
    interval_seconds: float,
    once: bool = False,
    on_batch: Callable[[DispatchBatch], None] | None = None,
) -> None:
    if not 0.1 <= interval_seconds <= 300:
        raise ValueError("Dispatch polling requires an explicit bounded interval")
    while not stop.is_set():
        result = await dispatcher.run_once(worker_id, should_stop=stop.is_set)
        if on_batch is not None:
            on_batch(result)
        if once:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _main(
    settings: JobDispatchSettings,
    planner: DispatchPlanner,
    results: ResultPublisher,
    *,
    once: bool,
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # Avoid pool diagnostics leaking host/principal details before the CLI can
    # replace them with a stable unavailable message.
    pool_logger = logging.getLogger("psycopg.pool")
    previous_disabled = pool_logger.disabled
    pool_logger.disabled = True
    try:
        runtime = JobDispatchRuntime(settings, planner, results)
        await runtime.open()
        try:
            assert runtime.dispatcher is not None
            await run_dispatch_worker(
                runtime.dispatcher,
                settings.worker_id,
                stop,
                interval_seconds=settings.interval_seconds,
                once=once,
                on_batch=lambda batch: print(
                    json.dumps(batch_log(batch), separators=(",", ":")), flush=True
                ),
            )
        finally:
            await runtime.close()
    finally:
        pool_logger.disabled = previous_disabled
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dispatch persisted public Jobs through the execution gateway"
    )
    parser.add_argument("--once", action="store_true", help="Run one bounded pass, then exit")
    options = parser.parse_args()
    try:
        settings = JobDispatchSettings()
        planner, results = build_dispatch_dependencies(settings)
        asyncio.run(_main(settings, planner, results, once=options.once))
    except DispatchDependenciesUnavailableError:
        parser.exit(
            2,
            "Job dispatch unavailable: planner and result publisher are not assembled; "
            "no lease was claimed.\n",
        )
    except Exception:
        parser.exit(1, "Job dispatch unavailable; no lease was claimed.\n")


if __name__ == "__main__":
    main()
