"""Kill real controller processes at stop boundaries, then recover using only GET.

Only approved inert CPU containers are created. No model is loaded, no container
host port is published, and the protected 35B service must retain its start time.
Fault barriers live in a probe child; production accepts no fault injection flag.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from engine_probe_support import protected_start
from node_controller_fault_process import PHASES
from node_controller_process import ControllerProcess, wait_for_health

from speechnexus_node_controller.infrastructure.docker_api import DockerApi
from speechnexus_node_controller.infrastructure.docker_runtime import (
    INSTANCE_ID_LABEL,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    NODE_ID_LABEL,
)

_NODE = "node-stop-probe-1"
_TOKEN = "synthetic-stop-recovery-component-token"
_TEMPLATE = "template-stop-probe"
_DIGEST = "sha256:" + "1" * 64
_HERE = Path(__file__).resolve().parent


class ProbeError(Exception):
    """A fixed diagnostic code, never a daemon body, path or credential."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProbeError(code)


def body(instance_id: str, *, stop: bool) -> dict[str, Any]:
    return {
        "intent_id": "intent-stop" if stop else "intent-prepare",
        "target_node_id": _NODE,
        "pool_id": "auxiliary",
        "gateway_id": "gateway-probe",
        "instance_id": instance_id,
        "instance_generation": 1,
        "approved_template_id": _TEMPLATE,
        "binding_revision_id": "binding-probe",
        "manifest_id": "manifest-probe",
        "config_digest": _DIGEST,
        "action": "stop" if stop else "prepare",
        "fencing_token": 2 if stop else 1,
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
        "deadline_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }


def checkpoint_stage(path: Path) -> str | None:
    # Read-only and compatible with the old v1 store for the baseline counterexample.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_stop_checkpoints'"
        ).fetchone()
        if not exists:
            return None
        row = connection.execute(
            "SELECT record FROM runtime_stop_checkpoints WHERE intent_id = 'intent-stop'"
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    return (
        "released"
        if payload["released_at"] is not None
        else "exited"
        if payload["exited_at"] is not None
        else "targeted"
    )


def run_case(
    report: dict[str, Any],
    *,
    api: DockerApi,
    image: str,
    image_id: str,
    port: int,
    timeout_seconds: float,
) -> None:
    phase = report["phase"]
    instance = "instance-stop-probe-" + phase.replace("_", "-")
    labels = {
        MANAGED_BY_LABEL: MANAGED_BY_VALUE,
        NODE_ID_LABEL: _NODE,
        INSTANCE_ID_LABEL: instance,
    }
    executable = shutil.which("speechnexus-node-controller")
    require(executable is not None, "controller_entrypoint_missing")
    controller: ControllerProcess | None = None
    with tempfile.TemporaryDirectory(prefix="speechnexus-stop-recovery-") as directory:
        work = Path(directory)
        marker = work / "fault.ready"
        (work / "approved-templates.json").write_text(
            json.dumps(
                {
                    "templates": [
                        {
                            "template_id": _TEMPLATE,
                            "image_reference": image,
                            "image_digest": image_id,
                            "memory_architecture": "unified_memory",
                            "allowed_pool_ids": ["auxiliary"],
                            "max_peak_memory_bytes": 2 * 1024**3,
                            "container_memory_limit_bytes": 256 * 1024**2,
                            "readiness": {"kind": "process_running"},
                            "command": ["python", "-c", "import time; time.sleep(300)"],
                        }
                    ]
                }
            )
        )
        common: dict[str, Any] = {
            "workdir": work,
            "port": port,
            "socket_path": "/var/run/docker.sock",
            "node_id": _NODE,
            "component_token": _TOKEN,
        }
        headers = {"Authorization": "Bearer " + _TOKEN}
        prefix = f"/internal/v1/nodes/{_NODE}/instance-intents"
        stop_request = body(instance, stop=True)
        try:
            controller = ControllerProcess(
                command=[
                    sys.executable,
                    str(_HERE / "node_controller_fault_process.py"),
                    "--phase",
                    phase,
                    "--marker",
                    str(marker),
                ],
                log_path=work / "fault.log",
                **common,
            )
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=30, trust_env=False
            ) as client:
                wait_for_health(client, timeout_seconds=15)
                prepared = client.post(prefix, json=body(instance, stop=False))
                require(prepared.status_code == 200, "prepare_refused")
                require(prepared.json()["intent_state"] == "succeeded", "prepare_not_ready")
                original = api.find_container(labels)
                require(original is not None, "original_container_missing")
                assert original is not None
                with ThreadPoolExecutor(max_workers=1) as workers:
                    sending = workers.submit(client.post, prefix, json=stop_request)
                    deadline = time.monotonic() + timeout_seconds
                    while not marker.exists() and time.monotonic() < deadline:
                        require(not sending.done(), "stop_ended_before_fault_barrier")
                        time.sleep(0.02)
                    try:
                        require(marker.exists(), "fault_barrier_not_reached")
                        pending = client.get(prefix + "/intent-stop").json()
                        require(pending["stop_evidence"] is None, "premature_stop_proof")
                        report["pending_intent_state"] = pending["intent_state"]
                        report["checkpoint_stage_before_crash"] = checkpoint_stage(
                            work / "runtime-intents.sqlite3"
                        )
                        current = api.inspect_container(original.container_id)
                        report["container_state_before_crash"] = (
                            "absent" if current is None else current.status
                        )
                    finally:
                        controller.kill()
                        controller = None
                    with suppress(httpx.HTTPError):
                        sending.result(timeout=5)
                started = time.monotonic()
                controller = ControllerProcess(
                    command=[str(executable)],
                    log_path=work / "recovered.log",
                    **common,
                )
                wait_for_health(client, timeout_seconds=15)
                recovered: dict[str, Any] = {}
                methods: list[str] = []
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline:
                    response = client.get(prefix + "/intent-stop")
                    methods.append("GET")
                    require(response.status_code == 200, "original_stop_query_refused")
                    recovered = response.json()
                    if recovered["intent_state"] == "succeeded":
                        break
                    time.sleep(0.1)
                report["last_intent_state"] = recovered.get("intent_state")
                require(recovered.get("intent_state") == "succeeded", "stop_recovery_timeout")
                evidence = recovered["stop_evidence"]
                require(evidence is not None, "recovered_stop_proof_missing")
                require(
                    evidence["process_or_container_exited"]
                    and evidence["resources_released_confirmed"]
                    and evidence["runtime_container_id"] == original.container_id,
                    "recovered_stop_proof_invalid",
                )
                report["recovery_seconds"] = round(time.monotonic() - started, 3)
                report["recovery_methods"] = sorted(set(methods))
                report["stop_exit_code"] = evidence["exit_code"]
                report["stop_verified_at"] = evidence["verified_at"]
                report["original_container_removed"] = (
                    api.inspect_container(original.container_id) is None
                )
                require(report["original_container_removed"], "original_container_still_exists")
                replay = client.post(prefix, json=stop_request)
                require(replay.status_code == 200, "original_stop_replay_refused")
                require(replay.json()["idempotent_replay"], "original_stop_not_replayed")
                require(replay.json()["stop_evidence"] == evidence, "stop_proof_changed_on_replay")
                controller.stop()
                controller = ControllerProcess(
                    command=[str(executable)],
                    log_path=work / "final.log",
                    **common,
                )
                wait_for_health(client, timeout_seconds=15)
                final = client.get(prefix + "/intent-stop").json()
                report["stable_proof_after_replay_restart"] = final["stop_evidence"] == evidence
                require(
                    report["stable_proof_after_replay_restart"], "stop_proof_changed_on_restart"
                )
                report["passed"] = True
        finally:
            if controller is not None:
                controller.stop()
            for container_id in api.list_container_ids(labels):
                api.remove_container(container_id)
            report["leftovers"] = list(api.list_container_ids(labels))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18022)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    parser.add_argument("--image", default="python:3.14.9-slim-bookworm")
    parser.add_argument("--phase", choices=PHASES, action="append")
    args = parser.parse_args()
    start = protected_start()
    with socket.socket() as check:
        check.bind(("127.0.0.1", args.port))
    report: dict[str, Any] = {"cases": [], "image_reference": args.image}
    try:
        with DockerApi() as api:
            image = api.inspect_image(args.image)
            require(image is not None, "approved_probe_image_missing")
            assert image is not None
            report["image_id"] = image.image_id
            for phase in args.phase or PHASES:
                case: dict[str, Any] = {"phase": phase}
                report["cases"].append(case)
                run_case(
                    case,
                    api=api,
                    image=args.image,
                    image_id=image.image_id,
                    port=args.port,
                    timeout_seconds=args.timeout_seconds,
                )
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["failure_code"] = (
            str(error) if isinstance(error, ProbeError) else type(error).__name__
        )
    finally:
        report["protected_start_unchanged"] = protected_start() == start
        report["protected_started_at"] = start
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] and report["protected_start_unchanged"] else 1


if __name__ == "__main__":
    sys.exit(main())
