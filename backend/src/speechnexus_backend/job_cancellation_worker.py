"""Standalone cancellation reconciliation process with an explicit off switch."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal

from speechnexus_backend.job_cancellation_runtime import (
    CancellationRuntimeUnavailableError,
    JobCancellationRuntime,
    JobCancellationSettings,
    build_cancellation_runtime,
)


def _pass_summary(batch: object) -> str:
    """One line per bounded pass, so an operator can tell the two cases apart.

    A silent empty pass and a process that died before doing anything looked
    identical in the cluster: ``succeeded=1`` with no output (#1293). Every pass
    now prints one JSON line, including the pass that found nothing to do.
    """
    fields = ("claimed", "settled", "retried", "exhausted", "unavailable")
    summary: dict[str, int | str] = {name: int(getattr(batch, name, 0)) for name in fields}
    blocked = getattr(batch, "blocked", ())
    summary["blocked"] = len(blocked) if blocked is not None else 0
    summary["event"] = "job_cancellation_pass"
    return json.dumps(summary, sort_keys=True)


async def _main(runtime: JobCancellationRuntime, *, once: bool) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await runtime.open()
        try:
            if once:
                print(_pass_summary(await runtime.run_once()), flush=True)
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
