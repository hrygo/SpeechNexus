#!/usr/bin/env python3
"""Build the asset-inspector image and probe its runtime invariants.

Run this on an isolated Linux Docker host. The CI matrix builds the image on
native ``ubuntu-24.04`` and ``ubuntu-24.04-arm`` runners; no QEMU, binfmt or
cross-compilation is involved. The probe builds ``asset-inspector/Dockerfile``
from the frozen lock, starts one container with the deployment's hardening
flags, and exits non-zero on the first violated invariant:

1. the image builds through the frozen dependency path (``uv export
   --frozen`` plus ``uv pip sync --require-hashes``) and the builder's import
   check actually ran;
2. the container runs as uid/gid 10001, ``/app`` rejects writes, and both
   private tmpfs paths stay writable (a positive control, so "everything is
   read-only" cannot pass);
3. ``GET /healthz`` is 200 and ``GET /readyz`` is 200 with the frozen contract
   and recipe versions plus the deployment limits this run injected;
4. ``/usr/bin/prlimit`` exists, is executable, and enforces the configured
   address-space, CPU, file-size, descriptor and core ceilings on the real
   decoder child command;
5. one real decode of a minimal 5 ms PCM WAV through
   ``POST /internal/v1/inspection/audio`` returns ``status=complete`` with the
   exact byte count, digest and media facts, while a wrong bearer token is
   rejected with ``401``.

Deliberate non-goals -- this probe does NOT verify:

* the core-count-dependent memory envelope. The runner's core count differs
  from the reference deployment host and ``import numpy`` under a 256 MiB
  address-space limit is not reliably reproducible across hosts; that envelope
  stays a unit test on the real ``prlimit`` path.
* Compose assembly or the ``internal: true`` deployment network. The probe
  starts a single container on loopback with ``--network none``; service
  discovery, the private API-to-inspector network and the absence of outbound
  routes are separate deployment evidence.
* real-host GPU, physical topology, codec coverage beyond PCM WAV, FFmpeg
  system-call behavior on aarch64, or the API side of the private protocol.

``/readyz`` is an explicitly unfrozen operational endpoint (see
``asset-inspector/README.md``): the frozen contract
``contracts/internal/asset-inspection.openapi.yaml`` documents only the
inspection path. The expected ``contract_version`` and ``recipe_version``
therefore come from the frozen receipt schema this service reuses
(``contracts/internal/audio-inspection.schema.json``) instead of a second
literal copy in this file.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = "asset-inspector/Dockerfile"
RECEIPT_SCHEMA = "contracts/internal/audio-inspection.schema.json"

RUNTIME_UID = 10001
RUNTIME_GID = 10001

# Synthetic values for this run only. The token never corresponds to a real
# deployment credential and is never printed by this probe.
TOKEN = "synthetic-inspection-probe-token"
REQUEST_ID = "asset-inspector-image-probe"

# Deployment ceilings injected through the documented environment, then read
# back from `/readyz` and reproduced in the `prlimit` child report.
MAX_ENCODED_BYTES = 2 * 1024 * 1024
MAX_WALL_SECONDS = 7.5
MEMORY_BYTES = 256 * 1024 * 1024
CPU_SECONDS = 5
OUTPUT_BYTES = 4096
NOFILE_LIMIT = 64

# A minimal PCM WAV built here, not a checked-in fixture: 80 mono s16le samples
# at 16 kHz, exactly 5 ms.
SAMPLE_RATE_HZ = 16_000
CHANNELS = 1
SAMPLE_COUNT = 80
READY_TIMEOUT_SECONDS = 150

IDENTITY_SCRIPT = """
import errno, json, os, stat


def write(path):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        return {
            "written": False,
            "errno": error.errno,
            "code": errno.errorcode.get(error.errno, str(error.errno)),
        }
    os.close(descriptor)
    os.unlink(path)
    return {"written": True}


prlimit = "/usr/bin/prlimit"
try:
    metadata = os.stat(prlimit)
except OSError as error:
    prlimit_report = {"exists": False, "errno": error.errno}
else:
    prlimit_report = {
        "exists": True,
        "regular": stat.S_ISREG(metadata.st_mode),
        "mode": oct(metadata.st_mode & 0o777),
        "executable": os.access(prlimit, os.X_OK),
    }

print(
    json.dumps(
        {
            "uid": os.getuid(),
            "gid": os.getgid(),
            "writes": {
                "/app/.probe": write("/app/.probe"),
                "/app/.venv/.probe": write("/app/.venv/.probe"),
                "/run/inspection/.probe": write("/run/inspection/.probe"),
                "/tmp/.probe": write("/tmp/.probe"),
            },
            "prlimit": prlimit_report,
        }
    )
)
"""

HTTP_SCRIPT = """
import base64, json, os, urllib.error, urllib.request

BODY = base64.b64decode("__BODY__")
BASE = "http://127.0.0.1:8080"


def call(method, path, data=None, headers=None):
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return {
                "status": response.status,
                "headers": dict(response.headers),
                "body": response.read().decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": error.code,
            "headers": dict(error.headers),
            "body": error.read().decode("utf-8", "replace"),
        }


HEADERS = {
    "Content-Type": "application/octet-stream",
    "Content-Length": str(len(BODY)),
    "X-Nexus-Deadline-Ms": "5000",
    "X-Nexus-Max-Duration-Ms": "1000",
    "X-Request-ID": "__REQUEST_ID__",
}

print(
    json.dumps(
        {
            "healthz": call("GET", "/healthz"),
            "readyz": call("GET", "/readyz"),
            "unauthorized": call(
                "POST",
                "/internal/v1/inspection/audio",
                BODY,
                dict(HEADERS, Authorization="Bearer synthetic-wrong-token"),
            ),
            "inspection": call(
                "POST",
                "/internal/v1/inspection/audio",
                BODY,
                dict(
                    HEADERS,
                    Authorization="Bearer " + os.environ["SPEECHNEXUS_INSPECTION_TOKEN"],
                ),
            ),
        }
    )
)
"""


class ProbeError(RuntimeError):
    """A runtime invariant did not hold; the probe must fail non-zero."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeError(message)


def passed(message: str) -> None:
    print(f"PASS {message}", flush=True)


def run(command: list[str], *, timeout: int = 900, env: dict[str, str] | None = None) -> str:
    environment = dict(os.environ)
    environment.setdefault("DOCKER_BUILDKIT", "1")
    if env:
        environment.update(env)
    result = subprocess.run(
        command, text=True, capture_output=True, timeout=timeout, env=environment
    )
    output = result.stdout + result.stderr
    if result.returncode:
        raise ProbeError(f"command failed ({result.returncode}): {' '.join(command)}\n{output}")
    return output


def exec_command(container: str, argv: list[str], *, env: dict[str, str] | None = None) -> str:
    command = ["docker", "exec"]
    for key, value in (env or {}).items():
        command.extend(["--env", f"{key}={value}"])
    command.extend([container, *argv])
    return run(command, timeout=120)


def exec_script(container: str, source: str) -> str:
    result = subprocess.run(
        ["docker", "exec", "--interactive", container, "/app/.venv/bin/python", "-"],
        input=source,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if result.returncode:
        raise ProbeError(
            f"in-container probe failed ({result.returncode})\n{result.stdout}{result.stderr}"
        )
    return result.stdout


def frozen_receipt_contract() -> tuple[int, str]:
    """Read the only frozen machine-readable version facts for this service.

    ``/readyz`` is an unfrozen operational endpoint, so the two version values
    it advertises are bound to the frozen receipt schema the inspection path
    reuses: the receipt ``schema_version`` is the private contract version and
    ``recipe_version`` is the fixed decode recipe. A drift between the image
    and this file fails the probe instead of passing a literal copy.
    """
    document = json.loads((ROOT / RECEIPT_SCHEMA).read_text(encoding="utf-8"))
    properties = document["$defs"]["AudioInspectionSuccess"]["properties"]
    return int(properties["schema_version"]["const"]), str(properties["recipe_version"]["const"])


def minimal_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as encoded:
        encoded.setnchannels(CHANNELS)
        encoded.setsampwidth(2)
        encoded.setframerate(SAMPLE_RATE_HZ)
        encoded.writeframes(b"\x00\x00" * SAMPLE_COUNT)
    return output.getvalue()


def build(image: str) -> str:
    output = run(
        [
            "docker",
            "build",
            "--no-cache",
            "--progress=plain",
            "--file",
            str(ROOT / DOCKERFILE),
            "--tag",
            image,
            str(ROOT),
        ],
        timeout=1200,
    )
    # The builder stage prints this only after the frozen dependency install
    # succeeded, so a cached or skipped install cannot satisfy the check.
    match = re.search(r"\bav \S+ numpy \S+\b", output)
    require(
        match is not None,
        "the builder's import check did not run, so the frozen install is unproven",
    )
    return match.group(0)


def start_container(container: str, image: str) -> None:
    run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            # Same hardening as the deployment unit: read-only root, private
            # tmpfs for the only writable paths, no capabilities, no new
            # privileges, bounded pids/memory/CPU and an init process.
            "--read-only",
            "--tmpfs",
            f"/run/inspection:rw,noexec,nosuid,nodev,size=64m,mode=0700,uid={RUNTIME_UID},gid={RUNTIME_GID}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "64",
            "--memory",
            "768m",
            "--cpus",
            "2.0",
            "--init",
            "--network",
            "none",
            "--env",
            f"SPEECHNEXUS_INSPECTION_TOKEN={TOKEN}",
            "--env",
            f"SPEECHNEXUS_INSPECTION_MAX_ENCODED_BYTES={MAX_ENCODED_BYTES}",
            "--env",
            f"SPEECHNEXUS_INSPECTION_MAX_WALL_SECONDS={MAX_WALL_SECONDS}",
            "--env",
            f"SPEECHNEXUS_INSPECTION_MEMORY_BYTES={MEMORY_BYTES}",
            "--env",
            f"SPEECHNEXUS_INSPECTION_CPU_SECONDS={CPU_SECONDS}",
            "--env",
            f"SPEECHNEXUS_INSPECTION_OUTPUT_BYTES={OUTPUT_BYTES}",
            image,
        ],
        timeout=120,
    )


def container_running(container: str) -> bool:
    return run(["docker", "inspect", "--format", "{{.State.Running}}", container]).strip() == "true"


def wait_until_ready(container: str) -> None:
    deadline = READY_TIMEOUT_SECONDS
    while deadline > 0:
        if not container_running(container):
            logs = subprocess.run(
                ["docker", "logs", container], text=True, capture_output=True, timeout=60
            )
            raise ProbeError(
                "the inspector exited before becoming ready\n" + logs.stdout + logs.stderr
            )
        probe = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "/app/.venv/bin/python",
                "-c",
                "import urllib.request; "
                "urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2)",
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if probe.returncode == 0:
            return
        time.sleep(1)
        deadline -= 1
    raise ProbeError(f"/readyz did not reach 200 within {READY_TIMEOUT_SECONDS} s")


def header(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def inspect_container(container: str) -> dict[str, Any]:
    return json.loads(exec_script(container, IDENTITY_SCRIPT))


def check_identity(report: dict[str, Any]) -> None:
    require(
        (report["uid"], report["gid"]) == (RUNTIME_UID, RUNTIME_GID),
        f"container runs as {report['uid']}:{report['gid']}, expected 10001:10001",
    )
    passed(f"container user is uid={report['uid']} gid={report['gid']}")

    for path in ("/app/.probe", "/app/.venv/.probe"):
        attempt = report["writes"][path]
        require(not attempt["written"], f"{path} was writable; /app must be read-only")
        require(
            attempt["errno"] in (errno.EACCES, errno.EROFS),
            f"{path} failed with {attempt['code']}, expected EACCES/EROFS",
        )
    passed("/app rejects writes (EROFS/EACCES for /app and /app/.venv)")

    for path in ("/run/inspection/.probe", "/tmp/.probe"):
        require(
            report["writes"][path]["written"],
            f"{path} was not writable, so the read-only check is not specific",
        )
    passed("the private tmpfs paths /run/inspection and /tmp are writable (positive control)")


def check_prlimit(container: str, report: dict[str, Any]) -> None:
    report = report["prlimit"]
    require(
        report["exists"] and report["regular"] and report["executable"],
        f"/usr/bin/prlimit is missing or not executable: {report}",
    )
    passed(f"/usr/bin/prlimit exists and is executable (mode {report['mode']})")

    # This is the exact wrapper shape `SubprocessRunner` builds for every decode
    # child; the child reports the ceilings it actually inherits.
    file_size_limit = OUTPUT_BYTES + 1
    output = exec_command(
        container,
        [
            "/usr/bin/prlimit",
            f"--as={MEMORY_BYTES}:{MEMORY_BYTES}",
            f"--cpu={CPU_SECONDS}:{CPU_SECONDS}",
            f"--fsize={file_size_limit}:{file_size_limit}",
            f"--nofile={NOFILE_LIMIT}:{NOFILE_LIMIT}",
            "--core=0:0",
            "--",
            "/app/.venv/bin/python",
            "-I",
            "-m",
            "speechnexus_asset_inspector.child",
            "--report-limits",
        ],
        env={
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )
    limits = json.loads(output.strip().splitlines()[-1])["limits"]
    expected = {
        "as": MEMORY_BYTES,
        "cpu": CPU_SECONDS,
        "fsize": file_size_limit,
        "nofile": NOFILE_LIMIT,
        "core": 0,
    }
    require(
        limits == expected,
        f"prlimit did not apply the configured ceilings: {limits} != {expected}",
    )
    passed(
        f"prlimit enforced the configured ceilings on the real decoder child: {json.dumps(limits)}"
    )


def check_http(
    container: str, expected_contract_version: int, expected_recipe_version: str
) -> None:
    body = minimal_wav()
    expected_duration_ms = (SAMPLE_COUNT * 1000 + SAMPLE_RATE_HZ - 1) // SAMPLE_RATE_HZ
    source = HTTP_SCRIPT.replace("__BODY__", base64.b64encode(body).decode("ascii")).replace(
        "__REQUEST_ID__", REQUEST_ID
    )
    responses: dict[str, dict[str, Any]] = json.loads(exec_script(container, source))

    healthz = responses["healthz"]
    require(healthz["status"] == 200, f"/healthz returned {healthz['status']}")
    require(
        json.loads(healthz["body"]) == {"status": "ok"}, "/healthz body is not the liveness fact"
    )
    passed('GET /healthz -> 200 {"status": "ok"}')

    readyz = responses["readyz"]
    require(readyz["status"] == 200, f"/readyz returned {readyz['status']}: {readyz['body']}")
    document = json.loads(readyz["body"])
    require(
        set(document)
        == {
            "contract_version",
            "recipe_version",
            "max_encoded_bytes",
            "max_wall_seconds",
            "in_flight",
        },
        f"/readyz exposes unexpected fields: {sorted(document)}",
    )
    require(
        document["contract_version"] == expected_contract_version,
        f"/readyz contract_version={document['contract_version']}, "
        f"frozen receipt schema_version={expected_contract_version}",
    )
    require(
        document["recipe_version"] == expected_recipe_version,
        f"/readyz recipe_version={document['recipe_version']}, "
        f"frozen recipe_version={expected_recipe_version}",
    )
    require(
        document["max_encoded_bytes"] == MAX_ENCODED_BYTES,
        f"/readyz max_encoded_bytes={document['max_encoded_bytes']} "
        f"!= injected {MAX_ENCODED_BYTES}",
    )
    require(
        document["max_wall_seconds"] == MAX_WALL_SECONDS,
        f"/readyz max_wall_seconds={document['max_wall_seconds']} != injected {MAX_WALL_SECONDS}",
    )
    require(document["in_flight"] == 0, f"/readyz in_flight={document['in_flight']} at rest")
    passed(f"GET /readyz -> 200 {json.dumps(document)}")

    unauthorized = responses["unauthorized"]
    require(unauthorized["status"] == 401, f"wrong token returned {unauthorized['status']}")
    failure = json.loads(unauthorized["body"])
    require(
        failure.get("status") == "unauthorized",
        f"wrong token body is not the unauthorized envelope: {failure}",
    )
    passed('POST /internal/v1/inspection/audio with a wrong token -> 401 "unauthorized"')

    inspection = responses["inspection"]
    require(
        inspection["status"] == 200,
        f"real decode returned {inspection['status']}: {inspection['body']}",
    )
    require(
        header(inspection["headers"], "content-type").startswith("application/json"),
        "the decode receipt is not application/json",
    )
    require(
        header(inspection["headers"], "x-request-id") == REQUEST_ID,
        "the decode receipt did not echo X-Request-ID",
    )
    receipt = json.loads(inspection["body"])
    expected_receipt = {
        "schema_version": expected_contract_version,
        "status": "complete",
        "recipe_version": expected_recipe_version,
        "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
        "encoded_bytes": len(body),
        "media_type": "audio/wav",
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "channels": CHANNELS,
        "source_samples": SAMPLE_COUNT,
        "duration_ms": expected_duration_ms,
    }
    require(
        receipt == expected_receipt,
        f"decode receipt mismatch:\n  actual   {json.dumps(receipt, sort_keys=True)}\n"
        f"  expected {json.dumps(expected_receipt, sort_keys=True)}",
    )
    passed(
        f"real decode: status=complete duration_ms={receipt['duration_ms']} "
        f"encoded_bytes={receipt['encoded_bytes']} source_samples={receipt['source_samples']} "
        f"sha256={receipt['sha256']}"
    )


def cleanup(container: str, image: str) -> None:
    removed = subprocess.run(
        ["docker", "rm", "--force", container], text=True, capture_output=True, timeout=60
    )
    if removed.returncode == 0:
        passed(f"removed probe container {container}")
    elif "No such container" not in removed.stderr:
        print(f"WARNING could not remove probe container: {removed.stderr.strip()}", flush=True)
    subprocess.run(
        ["docker", "image", "rm", "--force", image], text=True, capture_output=True, timeout=120
    )


def main() -> int:
    expected_contract_version, expected_recipe_version = frozen_receipt_contract()
    image = f"speechnexus-asset-inspector-ci:{os.getpid()}"
    container = f"speechnexus-asset-inspector-probe-{os.getpid()}"
    try:
        print(
            "Docker:",
            run(
                ["docker", "version", "--format", "{{.Client.Version}} {{.Server.Version}}"]
            ).strip(),
            flush=True,
        )
        builder_check = build(image)
        passed(
            f"image built through the frozen lock; builder import check reported {builder_check}"
        )
        start_container(container, image)
        wait_until_ready(container)
        passed(
            "container reached /readyz 200 after the startup self-check decoded its built-in WAV"
        )
        identity = inspect_container(container)
        check_identity(identity)
        check_prlimit(container, identity)
        check_http(container, expected_contract_version, expected_recipe_version)
    except ProbeError as error:
        print(f"FAIL {error}", flush=True)
        return 1
    finally:
        cleanup(container, image)
    print("ASSET INSPECTOR IMAGE PROBE PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
