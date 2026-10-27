"""Console entry point for the speech backend."""

from __future__ import annotations

import uvicorn

from speechnexus_backend.settings import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "speechnexus_backend.transcription_runtime:create_app_from_env",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=False,
        server_header=False,
    )
