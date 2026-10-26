"""Actual TCP consumers of source-bound SDKs; no Workbench business-route claim."""

from __future__ import annotations

import copy
import json
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.workbench_http import (
    iter_events,
    validate_request,
    validate_response,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/contracts"
EVENTS = json.loads((FIXTURES / "workbench-sse.json").read_text())["events"]
ERROR = next(
    e["value"]["body"]
    for e in json.loads((FIXTURES / "workbench-binary-http.json").read_text())["examples"]
    if e["status"] == 404 and e["valid"]
)
SEED = "9007199254740993"


@contextmanager
def source_server() -> Iterator[tuple[str, list[Any], list[bool]]]:
    observed: list[Any] = []
    closed: list[bool] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: Any) -> None:
            pass

        def do_GET(self) -> None:
            observed.append(
                (self.path, self.headers.get("Range"), self.headers.get("Last-Event-ID"))
            )
            resource = self.path.split("/")[-2]
            if self.path.endswith("/content"):
                if resource == "export-stream":
                    self.emit(206, "application/octet-stream", b"\x00\xff\x01\x02", live=True)
                    return
                status = int(resource.removeprefix("export-"))
                body = (
                    b"\x00\xff"
                    if status == 200
                    else b"\x00\xff\x01\x02"
                    if status == 206
                    else json.dumps(ERROR).encode()
                )
                media = "application/octet-stream" if status in (200, 206) else "application/json"
                self.emit(status, media, body)
            elif self.path.endswith("/events"):
                cursor = self.headers.get("Last-Event-ID")
                if resource == "export_broken":
                    event = copy.deepcopy(EVENTS[0])
                    event.update(id=str(int(SEED) + 1), export_id=resource)
                    self.emit(200, "text/event-stream", self.frame(event) + b"data: {")
                elif cursor == str(int(SEED) + 2):
                    self.emit(410, "application/json", json.dumps(ERROR).encode())
                else:
                    assert resource == "export_demo" and cursor in (SEED, str(int(SEED) + 1))
                    event = copy.deepcopy(EVENTS[0 if cursor == SEED else 1])
                    event["id"] = str(int(cursor) + 1)
                    self.emit(200, "text/event-stream; charset=utf-8", self.frame(event), live=True)
            else:
                raise AssertionError("Unexpected fixture path")

        @staticmethod
        def frame(event: Any) -> bytes:
            return (
                f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
            ).encode()

        def emit(self, status: int, media: str, body: bytes, *, live: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Connection", "close")
            if media.startswith("text/event-stream"):
                self.send_header("Cache-Control", "no-cache")
            if status == 206:
                self.send_header("Content-Range", "bytes 0-3/8")
            if status == 416:
                self.send_header("Content-Range", "bytes */8")
            if not live:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            if live:
                self.connection.settimeout(3)
                try:
                    closed.append(self.connection.recv(1) == b"")
                except TimeoutError, OSError:
                    closed.append(False)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observed, closed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def python_consumer(base: str) -> None:
    with httpx.Client(base_url=base, follow_redirects=False, timeout=2) as client:
        for status in (200, 206, 401, 404, 409, 410, 416):
            request = {
                "path": {"export_id": f"export-{status}"},
                "headers": {"Range": "bytes=0-3"},
            }
            validate_request("downloadBookExport", request)
            reply = client.get(
                f"/v1/workbench/exports/export-{status}/content", headers=request["headers"]
            )
            body = reply.content if status in (200, 206) else reply.json()
            if status in (200, 206):
                assert body == (b"\x00\xff" if status == 200 else b"\x00\xff\x01\x02")
            envelope = {"body": body, "headers": dict(reply.headers)}
            validate_response("downloadBookExport", status, envelope)
        with client.stream(
            "GET", "/v1/workbench/exports/export-stream/content", headers={"Range": "bytes=0-3"}
        ) as response:
            first = next(response.iter_raw())
            assert first == b"\x00\xff\x01\x02"
            validate_response(
                "downloadBookExport", 206, {"body": first, "headers": dict(response.headers)}
            )
            # Context exit closes this live response, without waiting for EOF.
        cursor = SEED
        for _ in range(2):
            request = {"path": {"export_id": "export_demo"}, "headers": {"Last-Event-ID": cursor}}
            validate_request("streamExportEvents", request)
            with client.stream(
                "GET", "/v1/workbench/exports/export_demo/events", headers=request["headers"]
            ) as response:
                chunks = response.iter_bytes()
                validate_response(
                    "streamExportEvents", 200, {"body": chunks, "headers": dict(response.headers)}
                )
                events = iter_events("streamExportEvents", chunks, request, max_event_bytes=8192)
                event = next(events)
                assert event["id"] == str(int(cursor) + 1)
                cursor = event["id"]  # Only a verified, yielded event moves the original cursor.
                events.close()
        gone = client.get(
            "/v1/workbench/exports/export_demo/events", headers={"Last-Event-ID": cursor}
        )
        assert gone.status_code == 410
        validate_response("streamExportEvents", 410, {"body": gone.json()})
        request = {"path": {"export_id": "export_broken"}, "headers": {"Last-Event-ID": SEED}}
        with client.stream(
            "GET", "/v1/workbench/exports/export_broken/events", headers=request["headers"]
        ) as response:
            events = iter_events(
                "streamExportEvents", response.iter_bytes(), request, max_event_bytes=8192
            )
            verified = next(events)
            with pytest.raises(ContractValidationError, match=r"^Invalid SSE stream$"):
                next(events)
            assert verified["id"] == str(int(SEED) + 1)


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_native_tcp_download_sse_resume_expiry_and_cancel(language: str) -> None:
    with source_server() as (base, observed, closed):
        if language == "python":
            python_consumer(base)
        else:
            result = subprocess.run(
                ["node", str(FIXTURES / "workbench-streaming-http.mjs"), base],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert result.returncode == 0, result.stderr
        deadline = time.monotonic() + 3
        while len(closed) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert closed == [True, True, True]
        contents = [r for r in observed if r[0].endswith("/content")]
        assert len(contents) == 8 and all(r[1] == "bytes=0-3" for r in contents)
        requests = [r for r in observed if r[0].endswith("/events")]
        assert [r[2] for r in requests] == [SEED, str(int(SEED) + 1), str(int(SEED) + 2), SEED]
