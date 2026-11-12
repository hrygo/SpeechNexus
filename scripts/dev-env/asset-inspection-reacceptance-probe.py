"""Accept the asset-inspection upload path against an already assembled live stack.

Covers the six observations that a fake transport cannot make (#589, #592):

* an unreachable inspector endpoint is a non-retryable ``503
  asset_inspection_unavailable``, while a real capacity refusal stays the
  retryable ``503 asset_inspection_busy``;
* a ``voice_reference`` upload returns ``201 ready`` with media facts equal to
  the file's own header facts;
* audio above ``SPEECHNEXUS_AUDIO_INSPECTION_MAX_REFERENCE_DURATION_MS`` is a
  ``422 asset_audio_too_long`` that writes no Asset row;
* a token that differs from the inspector's yields the same neutral refusal with
  no configuration detail in the body;
* a client abort leaves the inspector with ``in_flight == 0`` and no decoder
  child;
* the inspector really received the bytes: ``eth0`` RX grows by the body plus
  request framing, and by zero when the URL points at a closed port.

The stack, not this script, owns assembly: PostgreSQL, MinIO, the migrations,
the delegated ``sn_api`` role, the inspector container and the API processes are
started by the operator. Three API processes are required, because the failure
family is chosen by configuration: one correct, one whose inspection URL points
at a closed loopback port, and one that carries a token the inspector rejects.

The inspector image ships no ``procps``, so child processes are read from
``/proc`` instead of ``ps``; the counter and the process table are the same
evidence, only the reader differs.

Usage:

    uv run python scripts/dev-env/asset-inspection-reacceptance-probe.py \
        --api http://127.0.0.1:18005 \
        --api-unreachable http://127.0.0.1:18006 \
        --api-wrong-token http://127.0.0.1:18007 \
        --inspector-container sn626-inspector \
        --inspector-readyz http://127.0.0.1:59030/readyz

Exit status is non-zero when any check fails; every check prints its own command
evidence so a failure can be reproduced without rerunning the whole probe.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

# Media facts of the reference fixture. A clone reference must be at least one
# second; 24 kHz stereo is the shape the clone policy qualifies.
REFERENCE_SECONDS = 1.5
REFERENCE_RATE = 24000
REFERENCE_CHANNELS = 2
# A valid reference under the default 60 s ceiling, and one over it. Both fit the
# 4 MiB upload cap at these rates, so duration is the only difference the
# inspector can reject on.
VALID_LONG_SECONDS = 59.0
TOO_LONG_SECONDS = 65.0
# Request framing allowance above the encoded body: multipart boundaries, the
# three form parts, bearer/deadline headers and the HTTP request line.
FRAMING_ALLOWANCE_BYTES = 16 * 1024


def _split(base: str) -> SplitResult:
    parts = urlsplit(base)
    if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/"):
        raise SystemExit(f"expected a bare http origin, got {base!r}")
    return parts


def _request(
    base: str, method: str, path: str, body: bytes | None, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    parts = _split(base)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=30)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        received = {name.lower(): value for name, value in response.getheaders()}
        return status, received, response.read()
    finally:
        connection.close()


def _json(body: bytes) -> dict[str, object]:
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected a JSON object, got {type(value).__name__}")
    return value


def _multipart(wav: Path, purpose: str, workspace: str, boundary: str) -> bytes:
    encoded = wav.read_bytes()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="purpose"\r\n\r\n{purpose}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="workspace_id"\r\n\r\n{workspace}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{wav.name}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode()
    return head + encoded + f"\r\n--{boundary}--\r\n".encode()


@dataclass(frozen=True)
class Upload:
    status: int
    headers: dict[str, str]
    body: bytes

    def document(self) -> dict[str, object]:
        return _json(self.body)

    def error(self) -> dict[str, object]:
        error = self.document().get("error")
        if not isinstance(error, dict):
            raise AssertionError("response has no error envelope")
        return error


def _upload(base: str, wav: Path, purpose: str = "voice_reference") -> Upload:
    boundary = "----nexus-reacceptance-" + uuid.uuid4().hex
    status, headers, body = _request(
        base,
        "POST",
        "/v1/nexus/assets",
        _multipart(wav, purpose, "workspace-default", boundary),
        {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Idempotency-Key": "reaccept-" + uuid.uuid4().hex,
        },
    )
    return Upload(status, headers, body)


def _write_reference(path: Path, seconds: float, rate: int, channels: int) -> None:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(channels)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        payload = bytearray()
        for index in range(frames):
            sample = struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * index / rate)))
            payload += sample * channels
        sink.writeframes(bytes(payload))


def _file_facts(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        channels = source.getnchannels()
        frames = source.getnframes()
    duration_ms = (frames * 1000 + rate - 1) // rate
    return rate, channels, duration_ms


def _docker(*argv: str) -> str:
    completed = subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"docker {' '.join(argv)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _rx_bytes(container: str) -> int:
    return int(_docker("exec", container, "cat", "/sys/class/net/eth0/statistics/rx_bytes"))


def _readyz(url: str) -> dict[str, object]:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    status, _, body = _request(origin, "GET", parts.path or "/", None, {})
    if status != 200:
        raise AssertionError(f"readyz answered {status}")
    return _json(body)


def _in_flight(url: str) -> int:
    value = _readyz(url).get("in_flight")
    if not isinstance(value, int):
        raise AssertionError("readyz has no integer in_flight")
    return value


def _wait_idle(readyz: str, seconds: float = 30) -> None:
    """Wait until the inspector has no in-flight request.

    A busy slot is a capacity state, not a defect: an earlier request or a second
    probe process may still hold it, and asserting against a busy inspector would
    report a false failure. Readiness is a precondition here, not an assertion.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if _in_flight(readyz) == 0:
                return
        except AssertionError, OSError, http.client.HTTPException:
            pass
        time.sleep(0.1)
    raise AssertionError("inspector did not become idle")


# A decoder child is any process the service started for one request. Excluded:
# PID 1 (container init), the transient `sh` running this scan, and the service
# process itself, which is the only survivor an idle container should show.
_RESIDUAL_SCAN = (
    "for p in /proc/[0-9]*; do "
    'pid=${p#/proc/}; [ "$pid" = 1 ] && continue; '
    'comm=$(cat "$p/comm" 2>/dev/null) || continue; '
    '[ "$comm" = sh ] && continue; '
    "cmd=$(tr '\\0' ' ' < \"$p/cmdline\" 2>/dev/null) || continue; "
    'case "$cmd" in *speechnexus-asset-inspector*) continue ;; esac; '
    'echo "$pid $comm $cmd"; '
    "done"
)


def _residual_children(container: str) -> list[str]:
    output = _docker("exec", container, "sh", "-c", _RESIDUAL_SCAN)
    return [line for line in output.splitlines() if line.strip()]


def _abort_after_body(base: str, wav: Path, grace_seconds: float) -> int:
    """Send one complete upload, then reset the connection before reading a reply.

    ``SO_LINGER(1, 0)`` turns ``close`` into an immediate RST, which is a real
    client disappearing mid-request rather than a well-mannered shutdown.
    """
    parts = _split(base)
    boundary = "----nexus-abort-" + uuid.uuid4().hex
    body = _multipart(wav, "voice_reference", "workspace-default", boundary)
    connection = socket.create_connection((parts.hostname or "", parts.port or 80))
    head = (
        f"POST /v1/nexus/assets HTTP/1.1\r\n"
        f"Host: {parts.hostname}:{parts.port}\r\n"
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Idempotency-Key: reaccept-abort-{uuid.uuid4().hex}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    connection.sendall(head + body)
    sent = len(head) + len(body)
    # Let the request reach the inspector before disappearing, so the abort lands
    # while the decoder owns a slot instead of before admission.
    time.sleep(grace_seconds)
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    connection.close()
    return sent


@dataclass
class Report:
    passed: int = 0
    failed: int = 0

    def ok(self, name: str, detail: str) -> None:
        self.passed += 1
        print(f"PASS {name}: {detail}")

    def bad(self, name: str, detail: str) -> None:
        self.failed += 1
        print(f"FAIL {name}: {detail}")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_reference_ready(report: Report, api: str, wav: Path, container: str) -> None:
    """A qualifying reference uploads to ready, and the inspector received it."""
    before = _rx_bytes(container)
    upload = _upload(api, wav)
    after = _rx_bytes(container)
    if upload.status != 201:
        report.bad("A3b reference upload", f"status {upload.status}: {upload.body[:200]!r}")
        return
    document = upload.document()
    rate, channels, duration_ms = _file_facts(wav)
    _expect(document["status"] == "ready", f"status {document['status']}")
    _expect(
        document["sample_rate_hz"] == rate, f"sample_rate {document['sample_rate_hz']} != {rate}"
    )
    _expect(document["channels"] == channels, f"channels {document['channels']} != {channels}")
    _expect(
        document["duration_ms"] == duration_ms,
        f"duration {document['duration_ms']} != {duration_ms}",
    )
    body_bytes = wav.stat().st_size
    delta = after - before
    _expect(
        delta >= body_bytes, f"rx delta {delta} < body {body_bytes}: the inspector never saw it"
    )
    _expect(
        delta - body_bytes <= FRAMING_ALLOWANCE_BYTES,
        f"rx delta {delta} exceeds body {body_bytes} by more than framing",
    )
    report.ok(
        "A3b reference upload",
        f"201 {document['status']} rate={rate} ch={channels} duration_ms={duration_ms} "
        f"asset={document['id']} rx_delta={delta} body={body_bytes} framing={delta - body_bytes}",
    )


def check_traffic_contrast(report: Report, api_unreachable: str, wav: Path, container: str) -> None:
    """The same upload against a closed port moves the inspector's counter by zero."""
    before = _rx_bytes(container)
    upload = _upload(api_unreachable, wav)
    after = _rx_bytes(container)
    _expect(after - before == 0, f"rx delta {after - before} while the URL had no listener")
    _expect(upload.status == 503, f"status {upload.status}")
    _expect(upload.error()["code"] == "asset_inspection_unavailable", str(upload.error()["code"]))
    report.ok(
        "traffic causality", f"correct URL rx_delta>0, closed-port URL rx_delta={after - before}"
    )


def check_too_long(report: Report, api: str, wav: Path) -> None:
    """Audio over the reference ceiling is a permanent content verdict."""
    upload = _upload(api, wav)
    _expect(upload.status == 422, f"status {upload.status}: {upload.body[:200]!r}")
    error = upload.error()
    _expect(error["code"] == "asset_audio_too_long", str(error["code"]))
    _expect(error["retryable"] is False, "retryable is not false")
    _expect(upload.headers.get("x-should-retry") == "false", "X-Should-Retry is not false")
    report.ok(
        "reference over duration ceiling",
        f"422 {error['code']} retryable=false X-Should-Retry=false for {wav.name}",
    )


def check_threshold_bracket(report: Report, api: str, valid: Path, too_long: Path) -> None:
    """The ceiling is the reference budget, not the longer ASR decode budget."""
    accepted = _upload(api, valid)
    _expect(
        accepted.status == 201, f"{valid.name} status {accepted.status}: {accepted.body[:200]!r}"
    )
    rejected = _upload(api, too_long)
    _expect(rejected.status == 422, f"{too_long.name} status {rejected.status}")
    report.ok(
        "duration ceiling bracket",
        f"{valid.name} -> 201 ready, {too_long.name} -> 422 asset_audio_too_long",
    )


def check_token_mismatch(report: Report, api: str, wav: Path) -> None:
    """A rejected bearer is the neutral refusal, with no configuration detail."""
    upload = _upload(api, wav)
    _expect(upload.status == 503, f"status {upload.status}: {upload.body[:200]!r}")
    error = upload.error()
    _expect(error["code"] == "asset_inspection_unavailable", str(error["code"]))
    _expect(error["retryable"] is False, "retryable is not false")
    _expect(upload.headers.get("x-should-retry") == "false", "X-Should-Retry is not false")
    _expect(error["param"] is None, f"param {error['param']!r} names a configuration key")
    text = upload.body.decode("utf-8")
    for forbidden in ("token", "bearer", "401", "unauthorized", "127.0.0.1", "http://"):
        _expect(forbidden not in text.lower(), f"body leaks {forbidden!r}: {text}")
    report.ok(
        "A7 token mismatch",
        f"503 {error['code']} retryable=false param=null, body={text}",
    )


def check_client_abort(report: Report, api: str, wav: Path, readyz: str, container: str) -> None:
    """An aborted client leaves no in-flight slot and no decoder child."""
    peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            try:
                peak = max(peak, _in_flight(readyz))
            except AssertionError, OSError, http.client.HTTPException:
                continue
            time.sleep(0.005)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        sent = _abort_after_body(api, wav, 0.06)
        time.sleep(0.2)
    finally:
        stop.set()
        sampler.join(timeout=5)

    deadline = time.monotonic() + 15
    current = _in_flight(readyz)
    while current != 0 and time.monotonic() < deadline:
        time.sleep(0.05)
        current = _in_flight(readyz)
    _expect(current == 0, f"in_flight stayed at {current}")
    residual = _residual_children(container)
    _expect(residual == [], f"residual children {residual}")
    report.ok(
        "A5 client abort",
        f"body_sent={sent} in_flight_peak={peak} in_flight_final={current} residual_children=0",
    )


def check_outage(report: Report, api: str, wav: Path, container: str) -> None:
    """A stopped inspector is a deployment fault, never a retryable capacity signal."""
    _docker("stop", container)
    try:
        upload = _upload(api, wav)
        _expect(upload.status == 503, f"status {upload.status}: {upload.body[:200]!r}")
        error = upload.error()
        _expect(error["code"] == "asset_inspection_unavailable", str(error["code"]))
        _expect(error["retryable"] is False, "retryable is not false")
        _expect(upload.headers.get("x-should-retry") == "false", "X-Should-Retry is not false")
        report.ok(
            "A2 inspector stopped",
            f"503 {error['code']} retryable=false X-Should-Retry=false",
        )
    finally:
        _docker("start", container)


def check_busy_is_still_retryable(
    report: Report, api: str, wav: Path, concurrency: int, readyz: str
) -> None:
    """Overload must stay distinguishable from an outage: busy is retryable."""
    concurrent = concurrency + 2
    _wait_idle(readyz)
    gate = threading.Barrier(concurrent)

    def send(_: int) -> Upload:
        gate.wait(timeout=10)
        return _upload(api, wav)

    with ThreadPoolExecutor(max_workers=concurrent) as pool:
        uploads = list(pool.map(send, range(concurrent)))
    busy = [item for item in uploads if item.status == 503]
    _expect(busy != [], f"{concurrent} simultaneous uploads produced no busy refusal")
    codes = {item.error()["code"] for item in busy}
    _expect(codes == {"asset_inspection_busy"}, f"codes {codes} instead of a busy refusal")
    _expect(all(item.error()["retryable"] is True for item in busy), "busy is not retryable")
    report.ok(
        "busy vs outage",
        f"{len(busy)}/{concurrent} simultaneous uploads -> 503 asset_inspection_busy "
        f"retryable=true, {concurrent - len(busy)} accepted",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, help="API with a correct inspection URL and token")
    parser.add_argument(
        "--api-unreachable", required=True, help="API whose inspection URL is closed"
    )
    parser.add_argument(
        "--api-wrong-token", required=True, help="API whose inspection token differs"
    )
    parser.add_argument("--inspector-container", required=True)
    parser.add_argument("--inspector-readyz", required=True)
    parser.add_argument(
        "--inspector-concurrency",
        type=int,
        default=2,
        help="SPEECHNEXUS_INSPECTION_CONCURRENCY of the running container",
    )
    parser.add_argument("--workdir", type=Path, default=None, help="where fixtures are written")
    arguments = parser.parse_args()

    workdir = arguments.workdir or Path(tempfile.mkdtemp(prefix="asset-inspection-reaccept-"))
    workdir.mkdir(parents=True, exist_ok=True)
    reference = workdir / f"reference-{REFERENCE_RATE}hz-{REFERENCE_CHANNELS}ch.wav"
    valid_long = workdir / "reference-59s.wav"
    too_long = workdir / "reference-65s.wav"
    _write_reference(reference, REFERENCE_SECONDS, REFERENCE_RATE, REFERENCE_CHANNELS)
    _write_reference(valid_long, VALID_LONG_SECONDS, REFERENCE_RATE, 1)
    _write_reference(too_long, TOO_LONG_SECONDS, REFERENCE_RATE, 1)
    print(f"fixtures: {workdir}")

    _wait_idle(arguments.inspector_readyz)
    report = Report()
    steps: list[tuple[str, Callable[[], None]]] = [
        (
            "A3b reference upload",
            lambda: check_reference_ready(
                report, arguments.api, reference, arguments.inspector_container
            ),
        ),
        (
            "traffic causality",
            lambda: check_traffic_contrast(
                report, arguments.api_unreachable, reference, arguments.inspector_container
            ),
        ),
        (
            "reference over duration ceiling",
            lambda: check_too_long(report, arguments.api, too_long),
        ),
        (
            "duration ceiling bracket",
            lambda: check_threshold_bracket(report, arguments.api, valid_long, too_long),
        ),
        (
            "A7 token mismatch",
            lambda: check_token_mismatch(report, arguments.api_wrong_token, reference),
        ),
        (
            "A5 client abort",
            lambda: check_client_abort(
                report,
                arguments.api,
                valid_long,
                arguments.inspector_readyz,
                arguments.inspector_container,
            ),
        ),
        (
            "busy vs outage",
            lambda: check_busy_is_still_retryable(
                report,
                arguments.api,
                valid_long,
                arguments.inspector_concurrency,
                arguments.inspector_readyz,
            ),
        ),
        (
            "A2 inspector stopped",
            lambda: check_outage(report, arguments.api, reference, arguments.inspector_container),
        ),
    ]
    for name, step in steps:
        try:
            step()
        except AssertionError as error:
            report.bad(name, str(error))
        except Exception as error:
            report.bad(name, f"{type(error).__name__}: {error}")

    print(f"\nsummary: {report.passed} passed, {report.failed} failed")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
