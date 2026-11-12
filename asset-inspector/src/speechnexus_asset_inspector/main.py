"""Container entry point; startup fails closed on any missing hard limit."""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn

from speechnexus_asset_inspector.app import create_app
from speechnexus_asset_inspector.runner import SubprocessRunner
from speechnexus_asset_inspector.service import InspectorService
from speechnexus_asset_inspector.settings import ConfigurationError, InspectionSettings

RUNTIME_ROOT = Path("/run/inspection")
PRLIMIT = Path("/usr/bin/prlimit")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = InspectionSettings.from_env()
    except ConfigurationError as error:
        # The message names the variable, never its value.
        raise SystemExit(f"asset inspector configuration error: {error}") from None
    if not RUNTIME_ROOT.is_dir():
        raise SystemExit(f"asset inspector runtime root is missing: {RUNTIME_ROOT}")
    if not PRLIMIT.is_file():
        raise SystemExit(f"asset inspector prlimit is missing: {PRLIMIT}")
    runner = SubprocessRunner(settings, prlimit=PRLIMIT)
    service = InspectorService(settings, runtime_root=RUNTIME_ROOT, runner=runner)
    uvicorn.run(
        create_app(service),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
