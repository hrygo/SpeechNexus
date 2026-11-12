"""Synthetic bytes and app wiring shared by the inspector unit tests."""

from __future__ import annotations

import asyncio
import io
import struct
import wave
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from speechnexus_asset_inspector.runner import InspectionRunner, RunOutcome, SubprocessRunner
from speechnexus_asset_inspector.service import InspectorService
from speechnexus_asset_inspector.settings import InspectionSettings

TOKEN = "synthetic-inspection-bearer-token-0123456789"
PRLIMIT = Path("/usr/bin/prlimit")


def settings(**overrides: Any) -> InspectionSettings:
    base = InspectionSettings(token=TOKEN.encode("ascii"))
    return replace(base, **overrides)


def runner(value: InspectionSettings | None = None) -> SubprocessRunner:
    return SubprocessRunner(
        settings() if value is None else value,
        prlimit=PRLIMIT if PRLIMIT.is_file() else None,
    )


def pcm_wav(
    samples: int,
    *,
    sample_rate_hz: int = 16_000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as encoded:
        encoded.setnchannels(channels)
        encoded.setsampwidth(sample_width)
        encoded.setframerate(sample_rate_hz)
        encoded.writeframes(b"\x00" * (samples * channels * sample_width))
    return output.getvalue()


def float32_wav(samples: int, *, sample_rate_hz: int = 16_000, channels: int = 1) -> bytes:
    """A structurally valid non-PCM WAVE (IEEE float) the recipe must reject."""
    payload = struct.pack(f"<{samples * channels}f", *([0.0] * samples * channels))
    block_align = channels * 4
    fmt = struct.pack(
        "<HHIIHH",
        3,
        channels,
        sample_rate_hz,
        sample_rate_hz * block_align,
        block_align,
        32,
    )
    header = b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(payload)) + b"WAVE"
    return (
        header
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(payload))
        + payload
    )


async def wait_until(predicate: Callable[[], bool], *, budget: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not reached before the deadline")
        await asyncio.sleep(0.01)


class BlockingRunner:
    """A runner double for slot semantics; it never starts a decoder."""

    receipt = b'{"schema_version":1,"status":"complete"}'

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(
        self,
        *,
        input_fd: int,
        max_encoded_bytes: int,
        max_duration_ms: int,
        wall_seconds: float,
    ) -> RunOutcome:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return RunOutcome("complete", self.receipt)


def service(
    tmp_path: Path,
    *,
    value: InspectionSettings | None = None,
    runner_impl: InspectionRunner | None = None,
) -> InspectorService:
    resolved = settings() if value is None else value
    instance = InspectorService(
        resolved,
        runtime_root=tmp_path,
        runner=runner(resolved) if runner_impl is None else runner_impl,
    )
    instance.mark_ready()
    return instance


async def call_asgi(
    app: Any,
    *,
    headers: list[tuple[bytes, bytes]],
    body: bytes = b"",
    method: str = "POST",
    path: str = "/internal/v1/inspection/audio",
) -> tuple[int, dict[str, str], bytes]:
    """Call the ASGI app with exact raw header bytes, bypassing client defaults."""
    received: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        received.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("inspector", 8080),
        "root_path": "",
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in received if m["type"] == "http.response.start")
    raw_headers = next(m["headers"] for m in received if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in received if m["type"] == "http.response.body")
    header_map = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers}
    return int(status), header_map, payload


def inspection_headers(
    body: bytes,
    *,
    token: str = TOKEN,
    deadline_ms: int = 10_000,
    max_duration_ms: int = 120_000,
    content_length: int | None = None,
    content_type: str | None = "application/octet-stream",
    request_id: str | None = None,
) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"authorization", f"Bearer {token}".encode()),
        (b"content-length", str(len(body) if content_length is None else content_length).encode()),
        (b"x-nexus-deadline-ms", str(deadline_ms).encode()),
        (b"x-nexus-max-duration-ms", str(max_duration_ms).encode()),
    ]
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    if request_id is not None:
        headers.append((b"x-request-id", request_id.encode()))
    return headers
