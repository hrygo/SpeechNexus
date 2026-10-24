"""Console entry point for the restricted node controller."""

from __future__ import annotations

import uvicorn

from speechnexus_node_controller.settings import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "speechnexus_node_controller.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=False,
        server_header=False,
        workers=1,
    )
