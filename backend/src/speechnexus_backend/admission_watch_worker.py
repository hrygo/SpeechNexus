"""Standalone Job admission watcher with an explicit off switch."""

from __future__ import annotations

import argparse
import asyncio
import signal
from collections.abc import Sequence

from pydantic import ValidationError

from speechnexus_backend.admission_watch_runtime import (
    AdmissionWatchRuntime,
    AdmissionWatchSettings,
    AdmissionWatchUnavailableError,
    build_admission_watch,
)
from speechnexus_backend.speech.application.admission_watch import AdmissionWatchObservation

EXIT_ADMITTING = 0
EXIT_DEGRADED = 1
EXIT_UNAVAILABLE = 2


def exit_code(observations: tuple[AdmissionWatchObservation, ...]) -> int:
    """Non-zero while any watched workspace refuses every create.

    A one-shot poll is the whole surface a timer needs: the verdict is printed as the
    structured log line the loop writes, and this code lets cron/systemd act on it.
    """
    if any(not observation.report.admitting for observation in observations):
        return EXIT_DEGRADED
    return EXIT_ADMITTING


async def _main(runtime: AdmissionWatchRuntime, *, once: bool) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await runtime.open()
        try:
            if once:
                return exit_code(await runtime.poll_once())
            await runtime.run(stop)
            return EXIT_ADMITTING
        finally:
            await runtime.close()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Log changes in whether a public Job create would be admitted"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once, then exit (0 admitting, 1 degraded, 2 not configured or unreadable)",
    )
    options = parser.parse_args(argv)
    try:
        settings = AdmissionWatchSettings()
    except ValidationError as error:
        # Pydantic renders the missing keys and never the values; an operator who
        # started the profile with a blank DSN needs the name, not "unavailable".
        parser.exit(EXIT_UNAVAILABLE, f"Admission watch is not configured: {error}\n")
    runtime = build_admission_watch(settings)
    if runtime is None:
        print("admission watch disabled", flush=True)
        return
    try:
        code = asyncio.run(_main(runtime, once=options.once))
    except AdmissionWatchUnavailableError as error:
        parser.exit(EXIT_UNAVAILABLE, f"Admission watch is not configured: {error}\n")
    except Exception:
        parser.exit(EXIT_UNAVAILABLE, "Admission watch unavailable.\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
