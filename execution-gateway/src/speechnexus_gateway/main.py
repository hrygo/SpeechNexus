"""Console entry point for the execution gateway."""

from __future__ import annotations

import uvicorn

from speechnexus_gateway.settings import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "speechnexus_gateway.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=False,
        server_header=False,
    )
