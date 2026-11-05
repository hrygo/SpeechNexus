"""Standalone cancellation reconciliation process with an explicit off switch."""

from __future__ import annotations

import argparse
import asyncio
import signal

from speechnexus_backend.job_cancellation_runtime import (
    CancellationRuntimeUnavailableError,
    JobCancellationRuntime,
    JobCancellationSettings,
    build_cancellation_runtime,
)


async def _main(runtime: JobCancellationRuntime, *, once: bool) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await runtime.open()
        try:
            if once:
                await runtime.run_once()
            else:
                await runtime.run(stop)
        finally:
            await runtime.close()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Confirm accepted public Job cancellations against execution facts"
    )
    parser.add_argument("--once", action="store_true", help="Run one bounded pass, then exit")
    options = parser.parse_args()
    try:
        settings = JobCancellationSettings()
        runtime = build_cancellation_runtime(settings)
        if runtime is None:
            print("job cancellation reconciliation disabled", flush=True)
            return
        asyncio.run(_main(runtime, once=options.once))
    except CancellationRuntimeUnavailableError:
        parser.exit(2, "Job cancellation reconciliation is not configured.\n")
    except Exception:
        parser.exit(1, "Job cancellation reconciliation unavailable.\n")


if __name__ == "__main__":
    main()
