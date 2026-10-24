"""Probe the served node controller process end to end.

The probe starts the real ``speechnexus-node-controller`` process with a
synthetic approved template, a real durable intent store, and the local
container daemon, drives ``prepare`` → ``ready`` → ``stop`` over
``internal/runtime-control``, restarts the process to prove the recorded intent
survives, and removes the container it created. It loads no model, touches no
GPU service, publishes no host port, and uses only synthetic identifiers; the
JSON it prints is the reproducible evidence in the B02 audit note.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from speechnexus_node_controller.infrastructure.docker_api import DockerApi
from speechnexus_node_controller.infrastructure.docker_runtime import (
    INSTANCE_ID_LABEL,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
)

_NODE_ID = "node-local-1"
_INSTANCE_ID = "instance-probe-inert"
_TEMPLATE_ID = "template-probe-inert-tcp"
_CONFIG_DIGEST = "sha256:" + "1" * 64
_READINESS_PORT = 8080
_COMPONENT_TOKEN = "probe-component-token-0123456789"
_DEADLINE_SECONDS = 300.0


@dataclass(slots=True)
class ProbeReport:
    """Evidence the probe prints; every field is measured, not assumed."""

    image_reference: str = ""
    image_id: str = ""
    controller_command: str = ""
    health_status: int = 0
    unauthenticated_status: int = 0
    foreign_node_status: int = 0
    container_id: str = ""
    container_hardening: dict[str, Any] = field(default_factory=dict)
    prepare_status: int = 0
    prepare_state: str = ""
    ready_seconds: float | None = None
    observed_peak_bytes: int | None = None
    stop_status: int = 0
    stop_state: str = ""
    stop_seconds: float | None = None
    stop_exit_code: int | None = None
    stop_verified_at: str = ""
    container_removed: bool = False
    restart_preserved: bool = False
    intents_after_restart: list[dict[str, str]] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "image_reference": self.image_reference,
            "image_id": self.image_id,
            "controller_command": self.controller_command,
            "health_status": self.health_status,
            "unauthenticated_status": self.unauthenticated_status,
            "foreign_node_status": self.foreign_node_status,
            "container_id": self.container_id,
            "container_hardening": self.container_hardening,
            "prepare_status": self.prepare_status,
            "prepare_state": self.prepare_state,
            "ready_seconds": self.ready_seconds,
            "observed_peak_bytes": self.observed_peak_bytes,
            "stop_status": self.stop_status,
            "stop_state": self.stop_state,
            "stop_seconds": self.stop_seconds,
            "stop_exit_code": self.stop_exit_code,
            "stop_verified_at": self.stop_verified_at,
            "container_removed": self.container_removed,
            "restart_preserved": self.restart_preserved,
            "intents_after_restart": self.intents_after_restart,
            "leftovers": self.leftovers,
        }


def _controller_command() -> list[str]:
    executable = shutil.which("speechnexus-node-controller")
    if executable is None:
        raise SystemExit(
            "speechnexus-node-controller is not on PATH; run this probe through `uv run`"
        )
    return [executable]


def _intent_body(*, intent_id: str, action: str, fencing_token: int) -> dict[str, Any]:
    return {
        "intent_id": intent_id,
        "target_node_id": _NODE_ID,
        "pool_id": "auxiliary",
        "gateway_id": "gateway-1",
        "instance_id": _INSTANCE_ID,
        "instance_generation": 1,
        "approved_template_id": _TEMPLATE_ID,
        "binding_revision_id": "binding-probe",
        "manifest_id": "manifest-probe",
        "config_digest": _CONFIG_DIGEST,
        "action": action,
        "fencing_token": fencing_token,
        "reserved_memory": {
            "architecture": "unified_memory",
            "peak": {
                "unified_memory_bytes": 1024**3,
                "device_memory_bytes": 0,
                "host_memory_bytes": 0,
            },
            "steady": {
                "unified_memory_bytes": 256 * 1024**2,
                "device_memory_bytes": 0,
                "host_memory_bytes": 0,
            },
        },
        "deadline_at": (datetime.now(UTC) + timedelta(seconds=_DEADLINE_SECONDS)).isoformat(),
    }


def _write_templates(path: Path, image_reference: str, image_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "templates": [
                    {
                        "template_id": _TEMPLATE_ID,
                        "image_reference": image_reference,
                        "image_digest": image_id,
                        "memory_architecture": "unified_memory",
                        "allowed_pool_ids": ["auxiliary"],
                        "max_peak_memory_bytes": 2 * 1024**3,
                        "container_memory_limit_bytes": 256 * 1024**2,
                        "readiness": {"kind": "tcp_port_accepting", "port": _READINESS_PORT},
                        "command": ["python", "-m", "http.server", str(_READINESS_PORT)],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


class ControllerProcess:
    """One real controller process, with the environment a deployment would set."""

    def __init__(
        self,
        *,
        command: list[str],
        workdir: Path,
        port: int,
        socket_path: str,
        log_path: Path,
    ) -> None:
        self._log = log_path.open("ab")
        self._process = subprocess.Popen(
            command,
            cwd=str(workdir),
            env={
                **os.environ,
                "SPEECHNEXUS_CONTROLLER_NODE_ID": _NODE_ID,
                "SPEECHNEXUS_CONTROLLER_COMPONENT_TOKEN": _COMPONENT_TOKEN,
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
            },
            stdout=self._log,
            stderr=self._log,
            start_new_session=True,
        )

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=15)
        self._log.close()


def _wait_for_health(client: httpx.Client, *, timeout_seconds: float) -> int:
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
    raise SystemExit(f"the controller did not become healthy within {timeout_seconds}s")


def _host_config_readout(socket_path: str, container_id: str) -> dict[str, Any]:
    """Read the daemon's own view of the container, for the hardening record."""
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=socket_path),
        base_url="http://docker-engine",
        timeout=10.0,
    ) as client:
        payload: dict[str, Any] = client.get(f"/containers/{container_id}/json").json()
    host = payload.get("HostConfig", {})
    config = payload.get("Config", {})
    return {
        "privileged": host.get("Privileged"),
        "cap_drop": host.get("CapDrop"),
        "security_opt": host.get("SecurityOpt"),
        "init": host.get("Init"),
        "restart_policy": host.get("RestartPolicy"),
        "port_bindings": host.get("PortBindings"),
        "network_mode": host.get("NetworkMode"),
        "image": config.get("Image"),
        "user": config.get("User"),
    }


def _intents_path(intent_id: str) -> str:
    return f"/internal/v1/nodes/{_NODE_ID}/instance-intents/{intent_id}"


def _await_terminal(
    client: httpx.Client,
    intent_id: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(_intents_path(intent_id), headers=headers)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        if body["intent_state"] in {"succeeded", "failed", "quarantined"}:
            return body
        time.sleep(0.5)
    raise SystemExit(f"intent {intent_id} did not reach a terminal state in {timeout_seconds}s")


def run_probe(
    *,
    socket_path: str,
    image_reference: str,
    ready_timeout_seconds: float,
    port: int,
) -> ProbeReport:
    """Run one prepare → ready → stop → restart cycle and always clean up."""
    report = ProbeReport(image_reference=image_reference)
    command = _controller_command()
    report.controller_command = Path(command[0]).name
    workdir = Path(tempfile.mkdtemp(prefix="speechnexus-controller-probe-"))
    api = DockerApi(socket_path=socket_path)
    controller: ControllerProcess | None = None
    headers = {"Authorization": f"Bearer {_COMPONENT_TOKEN}"}
    try:
        api.ping()
        identity = api.inspect_image(image_reference)
        if identity is None:
            raise SystemExit(
                f"image {image_reference} is not present locally; pull it before probing"
            )
        report.image_id = identity.image_id
        _write_templates(workdir / "approved-templates.json", image_reference, identity.image_id)

        controller = ControllerProcess(
            command=command,
            workdir=workdir,
            port=port,
            socket_path=socket_path,
            log_path=workdir / "controller.log",
        )
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            report.health_status = _wait_for_health(client, timeout_seconds=30.0)

            report.unauthenticated_status = client.get(
                _intents_path("intent-probe-prepare")
            ).status_code
            report.foreign_node_status = client.get(
                "/internal/v1/nodes/node-other/instance-intents/intent-probe-prepare",
                headers=headers,
            ).status_code

            started = time.monotonic()
            prepared = client.post(
                f"/internal/v1/nodes/{_NODE_ID}/instance-intents",
                json=_intent_body(
                    intent_id="intent-probe-prepare", action="prepare", fencing_token=1
                ),
                headers=headers,
            )
            report.prepare_status = prepared.status_code
            if prepared.status_code != 200:
                raise SystemExit(f"prepare was refused: {prepared.text}")
            report.prepare_state = prepared.json()["observed_instance_state"]
            ready = _await_terminal(
                client,
                "intent-probe-prepare",
                headers=headers,
                timeout_seconds=ready_timeout_seconds,
            )
            report.ready_seconds = round(time.monotonic() - started, 3)
            report.prepare_state = ready["intent_state"]
            peak = ready.get("observed_peak_memory")
            if isinstance(peak, dict):
                report.observed_peak_bytes = peak["unified_memory_bytes"]

            found = api.find_container(
                {MANAGED_BY_LABEL: MANAGED_BY_VALUE, INSTANCE_ID_LABEL: _INSTANCE_ID}
            )
            if found is None:
                raise SystemExit("the prepared container could not be found on this node")
            report.container_id = found.container_id
            report.container_hardening = _host_config_readout(socket_path, found.container_id)

            stop_started = time.monotonic()
            stopped = client.post(
                f"/internal/v1/nodes/{_NODE_ID}/instance-intents",
                json=_intent_body(intent_id="intent-probe-stop", action="stop", fencing_token=2),
                headers=headers,
            )
            report.stop_status = stopped.status_code
            report.stop_seconds = round(time.monotonic() - stop_started, 3)
            if stopped.status_code != 200:
                raise SystemExit(f"stop was refused: {stopped.text}")
            stop_body = stopped.json()
            report.stop_state = stop_body["intent_state"]
            evidence = stop_body.get("stop_evidence")
            if isinstance(evidence, dict):
                report.stop_exit_code = evidence["exit_code"]
                report.stop_verified_at = evidence["verified_at"]

            report.container_removed = api.inspect_container(found.container_id) is None

        # A second process with the same store must still know the recorded intents.
        controller.stop()
        controller = ControllerProcess(
            command=command,
            workdir=workdir,
            port=port,
            socket_path=socket_path,
            log_path=workdir / "controller-restart.log",
        )
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            _wait_for_health(client, timeout_seconds=30.0)
            survivors = [
                client.get(_intents_path(intent_id), headers=headers).json()
                for intent_id in ("intent-probe-prepare", "intent-probe-stop")
            ]
        report.restart_preserved = all(
            intent["intent_state"] == "succeeded" for intent in survivors
        )
        report.intents_after_restart = [
            {
                "intent_id": intent["intent_id"],
                "intent_state": intent["intent_state"],
                "observed_instance_state": intent["observed_instance_state"],
            }
            for intent in survivors
        ]
        report.leftovers = list(
            api.list_container_ids(
                {MANAGED_BY_LABEL: MANAGED_BY_VALUE, INSTANCE_ID_LABEL: _INSTANCE_ID}
            )
        )
    finally:
        if controller is not None:
            controller.stop()
        for container_id in api.list_container_ids(
            {MANAGED_BY_LABEL: MANAGED_BY_VALUE, INSTANCE_ID_LABEL: _INSTANCE_ID}
        ):
            api.remove_container(container_id)
        api.close()
        shutil.rmtree(workdir, ignore_errors=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/var/run/docker.sock")
    parser.add_argument("--image", default="python:3.14.9-slim-bookworm")
    parser.add_argument("--ready-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--port", type=int, default=8021)
    arguments = parser.parse_args(argv)

    report = run_probe(
        socket_path=arguments.socket,
        image_reference=arguments.image,
        ready_timeout_seconds=arguments.ready_timeout_seconds,
        port=arguments.port,
    )
    print(json.dumps(report.as_json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
