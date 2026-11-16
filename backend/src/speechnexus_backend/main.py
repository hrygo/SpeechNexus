"""Console entry point for the speech backend."""

from __future__ import annotations

import sys

import uvicorn

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    SettlementPreflightUnavailableError,
)


def main() -> None:
    settings = Settings()
    try:
        uvicorn.run(
            "speechnexus_backend.transcription_runtime:create_app_from_env",
            factory=True,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level,
            proxy_headers=False,
            server_header=False,
        )
    except SettlementPreflightUnavailableError as refusal:
        # A database this process can reach but never migrated is a deployment
        # defect, not an exception to trace: print the named repair once and exit
        # non-zero so a supervisor's restart loop reports the same line instead of
        # a stack trace with no instruction (#877 item 3).
        #
        # This handler covers the refusals raised while uvicorn *loads* the
        # application string -- ``build_speech()`` in ``speech_runtime`` runs
        # synchronously there, so its exception propagates out of ``uvicorn.run``.
        # The Job writer's schema preflight is NOT one of them: it runs inside the
        # ASGI lifespan (``app.create_app`` awaits ``JobRuntime.open()``), where
        # uvicorn logs the exception with its traceback and ends the process with
        # exit code 3 (``STARTUP_FAILURE``) from ``Server.startup()``. Catching it
        # here would be unreachable, so the startup answer for an unmigrated Job
        # database is that named exception in uvicorn's output, not this line
        # (#920 item 3, review M1).
        print(f"FAIL: {refusal}", file=sys.stderr)
        raise SystemExit(1) from None
