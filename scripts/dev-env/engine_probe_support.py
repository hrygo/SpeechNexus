"""Read-only host facts shared by the controller lifecycle probes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from speechnexus_node_controller.domain.templates import ApprovedRuntimeTemplate

PROTECTED_CONTAINER = "cube-ccba-qwen36-35b-nvfp4"


def docker_json(*arguments: str) -> Any:
    result = subprocess.run(
        ["docker", *arguments], check=True, capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)


def protected_start() -> str:
    """Return the protected LLM's start time; it must already be running."""
    state = docker_json("inspect", PROTECTED_CONTAINER)[0]["State"]
    if not state["Running"]:
        raise RuntimeError("the protected LLM must already be running")
    return str(state["StartedAt"])


def require_internal_network(template: ApprovedRuntimeTemplate) -> None:
    """Refuse a template whose network is not an internal bridge."""
    if template.network_name is None:
        raise ValueError("the probe requires an explicitly approved internal network")
    network = docker_json("network", "inspect", template.network_name)[0]
    if not network.get("Internal") or network.get("Driver") != "bridge":
        raise ValueError("the probe requires a Docker --internal bridge network")


def memory_available() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("the host did not report MemAvailable")
