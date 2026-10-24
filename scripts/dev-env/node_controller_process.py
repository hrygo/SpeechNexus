"""Process ownership shared by CPU-only controller probes."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import httpx


class ControllerProcess:
    """Own one real controller process and its private log."""

    def __init__(
        self,
        *,
        command: list[str],
        workdir: Path,
        port: int,
        socket_path: str,
        log_path: Path,
        node_id: str,
        component_token: str,
    ) -> None:
        self._log = log_path.open("ab")
        try:
            self._process = subprocess.Popen(
                command,
                cwd=str(workdir),
                env={
                    **os.environ,
                    "SPEECHNEXUS_CONTROLLER_NODE_ID": node_id,
                    "SPEECHNEXUS_CONTROLLER_COMPONENT_TOKEN": component_token,
                    "SPEECHNEXUS_CONTROLLER_APPROVED_TEMPLATES_FILE": str(
                        workdir / "approved-templates.json"
                    ),
                    "SPEECHNEXUS_CONTROLLER_INTENT_STORE_PATH": str(
                        workdir / "runtime-intents.sqlite3"
                    ),
                    "SPEECHNEXUS_CONTROLLER_DOCKER_SOCKET_PATH": socket_path,
                    "SPEECHNEXUS_CONTROLLER_HOST": "127.0.0.1",
                    "SPEECHNEXUS_CONTROLLER_PORT": str(port),
                    "SPEECHNEXUS_CONTROLLER_LOG_LEVEL": "warning",
                    "SPEECHNEXUS_CONTROLLER_OBSERVATION_INTERVAL_SECONDS": "0.2",
                },
                stdout=self._log,
                stderr=self._log,
                start_new_session=True,
            )
        except BaseException:
            self._log.close()
            raise

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=15)
        self._log.close()

    def kill(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=15)
        self._log.close()


def wait_for_health(client: httpx.Client, *, timeout_seconds: float) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = client.get("/healthz")
        except httpx.HTTPError:
            time.sleep(0.2)
            continue
        if response.status_code == 200:
            return response.status_code
        time.sleep(0.2)
    raise RuntimeError("the controller did not become healthy before the probe deadline")
