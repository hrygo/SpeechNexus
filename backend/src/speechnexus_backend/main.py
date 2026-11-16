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
        print(f"FAIL: {refusal}", file=sys.stderr)
        raise SystemExit(1) from None
