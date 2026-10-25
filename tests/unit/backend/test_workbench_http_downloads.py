"""Real generated ASGI routes and private spool verification, without a speech engine."""

import asyncio
import hashlib
import io
import threading
from typing import Any, cast

import httpx
import pytest
from minio import Minio
from speechnexus_contracts.workbench_http import validate_response
from starlette.requests import ClientDisconnect

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.downloads import (
    DownloadContent,
    DownloadError,
    WorkbenchDownloads,
)
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.infrastructure.minio_object_zone import (
    MinioDownloadReader,
    MinioObjectZone,
)
from speechnexus_backend.workbench.interfaces.http_downloads import _DownloadResponse

CONTENT = b"\x00\xff\x01\x02\x03\x04\x05\x06"
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()


class ObjectReply(io.BytesIO):
    released = False

    def release_conn(self) -> None:
        self.released = True


class Objects:
    def __init__(self, body: bytes = CONTENT) -> None:
        self.body = body
        self.responses: list[ObjectReply] = []

    def get_object(self, bucket: str, key: str) -> ObjectReply:
        assert (bucket, key) == ("synthetic-bucket", "saved/project-demo/object-demo")
        reply = ObjectReply(self.body)
        self.responses.append(reply)
        return reply


class Store:
    calls = 0
    unavailable_after_copy = False

    async def content(self, *, kind: str, ident: str, workspace_id: str) -> DownloadContent:
        self.calls += 1
        assert kind in ("saved", "export")
        if ident == "unknown-demo" or (self.unavailable_after_copy and self.calls > 1):
            raise ResourceNotFoundError
        if ident == "expired-demo":
            raise DownloadError("download_expired")
        if ident == "pending-demo":
            raise DownloadError("download_not_ready")
        return DownloadContent("project-demo/object-demo", len(CONTENT), DIGEST)


def service(
    objects: Objects | None = None, *, limit: int = 20, timeout: float = 1
) -> tuple[WorkbenchDownloads, Objects, Store]:
    objects = objects or Objects()
    zone = MinioObjectZone(
        "http://127.0.0.1:59000", "synthetic-bucket", "access", "synthetic-secret"
    )
    zone._client = cast(Minio, objects)
    store = Store()
    return (
        WorkbenchDownloads(
            store, MinioDownloadReader(zone, max_bytes=limit, timeout_seconds=timeout), max_active=1
        ),
        objects,
        store,
    )


def client(downloads: WorkbenchDownloads | None) -> httpx.AsyncClient:
    app = create_app(
        None,
        default_workspace_id="workspace-demo",
        deployment_id="deployment-demo",
        cursor_key=b"synthetic-key-32-bytes-long-enough",
        max_json_bytes=4096,
        body_timeout_seconds=1,
        downloads=downloads,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic")


@pytest.mark.parametrize(
    ("prefix", "operation"),
    [("saved-assets", "downloadSavedAsset"), ("exports", "downloadBookExport")],
)
async def test_committed_downloads_and_ranges(prefix: str, operation: str) -> None:
    downloads, objects, _ = service()
    async with client(downloads) as c:
        for header, status, expected, content_range in [
            (None, 200, CONTENT, None),
            ("bytes=1-3", 206, CONTENT[1:4], "bytes 1-3/8"),
            ("bytes=5-", 206, CONTENT[5:], "bytes 5-7/8"),
            ("bytes=-2", 206, CONTENT[-2:], "bytes 6-7/8"),
        ]:
            headers = {"X-Request-ID": "synthetic-request"}
            if header:
                headers["Range"] = header
            reply = await c.get(f"/v1/workbench/{prefix}/content-demo/content", headers=headers)
            assert reply.status_code == status, reply.text
            assert reply.content == expected
            assert reply.headers.get("content-range") == content_range
            assert reply.headers["content-length"] == str(len(expected))
            assert reply.headers["etag"] == '"' + DIGEST + '"'
            assert reply.headers["accept-ranges"] == "bytes"
            assert reply.headers["x-request-id"] == "synthetic-request"
            validate_response(
                operation, status, {"body": reply.content, "headers": dict(reply.headers)}
            )
    assert all(response.closed and response.released for response in objects.responses)
    assert downloads._active == 0


@pytest.mark.parametrize(
    ("header", "status"),
    [("bytes=-", 400), ("bytes=8-", 416), ("bytes=3-1", 400), ("items=0-1", 400)],
)
async def test_range_failures_do_not_read_objects(header: str, status: int) -> None:
    downloads, objects, _ = service()
    async with client(downloads) as c:
        reply = await c.get(
            "/v1/workbench/saved-assets/content-demo/content", headers={"Range": header}
        )
    assert reply.status_code == status, reply.text
    if status == 416:
        assert reply.headers["content-range"] == "bytes */8"
    validate_response(
        "downloadSavedAsset", status, {"body": reply.json(), "headers": dict(reply.headers)}
    )
    assert not objects.responses and downloads._active == 0


@pytest.mark.parametrize(
    "body",
    [CONTENT[:-1], CONTENT + b"x", b"x" * len(CONTENT)],
    ids=["short", "long", "wrong-digest"],
)
async def test_corruption_never_sends_success_headers(body: bytes) -> None:
    downloads, objects, _ = service(Objects(body))
    async with client(downloads) as c:
        reply = await c.get(
            "/v1/workbench/saved-assets/content-demo/content", headers={"Range": "bytes=0-1"}
        )
    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "download_unavailable"
    assert "etag" not in reply.headers and "content-range" not in reply.headers
    assert downloads._active == 0
    assert all(r.closed and r.released for r in objects.responses)


async def test_deleted_during_copy_stays_hidden() -> None:
    downloads, objects, store = service()
    store.unavailable_after_copy = True
    async with client(downloads) as c:
        reply = await c.get("/v1/workbench/saved-assets/content-demo/content")
    assert reply.status_code == 404 and downloads._active == 0
    assert objects.responses[0].closed


@pytest.mark.parametrize(
    ("ident", "status"), [("unknown-demo", 404), ("expired-demo", 410), ("pending-demo", 409)]
)
async def test_resource_states_before_object_access(ident: str, status: int) -> None:
    downloads, objects, _ = service()
    async with client(downloads) as c:
        reply = await c.get(f"/v1/workbench/exports/{ident}/content")
    assert reply.status_code == status
    assert not objects.responses and downloads._active == 0


async def test_size_capacity_and_disabled_routes_are_bounded() -> None:
    downloads, objects, _ = service(limit=7)
    async with client(downloads) as c:
        assert (await c.get("/v1/workbench/saved-assets/content-demo/content")).status_code == 413
    assert not objects.responses and downloads._active == 0
    downloads, _, _ = service()
    prepared = await downloads.prepare(
        kind="saved", ident="content-demo", workspace_id="workspace-demo", range_header=None
    )
    async with client(downloads) as c:
        assert (await c.get("/v1/workbench/saved-assets/content-demo/content")).status_code == 503
    await prepared.close()
    await prepared.close()
    assert downloads._active == 0
    async with client(None) as c:
        assert (await c.get("/v1/workbench/saved-assets/content-demo/content")).status_code == 503


async def test_duplicate_ranges_are_not_silently_selected() -> None:
    downloads, objects, _ = service()
    async with client(downloads) as c:
        reply = await c.get(
            "/v1/workbench/saved-assets/content-demo/content",
            headers=[("Range", "bytes=0-1"), ("Range", "bytes=2-3")],
        )
    assert reply.status_code == 400 and not objects.responses


async def test_send_failure_before_iteration_closes_buffer_and_returns_capacity() -> None:
    downloads, _, _ = service()
    prepared = await downloads.prepare(
        kind="saved", ident="content-demo", workspace_id="workspace-demo", range_header=None
    )
    response = _DownloadResponse(prepared, "request-demo", timeout=1)

    async def send(message: Any) -> None:
        raise OSError("synthetic disconnected peer")

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert prepared.closed and downloads._active == 0


async def test_cancel_joins_native_read_before_releasing_capacity() -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class SlowReply(ObjectReply):
        def read(self, size: int | None = -1) -> bytes:
            started.set()
            assert release.wait(2)
            finished.set()
            return super().read(size)

    class SlowObjects(Objects):
        def get_object(self, bucket: str, key: str) -> ObjectReply:
            reply = SlowReply(CONTENT)
            self.responses.append(reply)
            return reply

    downloads, objects, _ = service(SlowObjects())
    task = asyncio.create_task(
        downloads.prepare(
            kind="saved", ident="content-demo", workspace_id="workspace-demo", range_header=None
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and downloads._active == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and downloads._active == 0
    assert objects.responses[0].closed and objects.responses[0].released


async def test_prepare_timeout_waits_for_socket_read_before_closing() -> None:
    entered, release = threading.Event(), threading.Event()

    class SlowReply(ObjectReply):
        def read(self, size: int | None = -1) -> bytes:
            entered.set()
            assert release.wait(2)
            assert not self.closed
            return super().read(size)

    class SlowObjects(Objects):
        def get_object(self, bucket: str, key: str) -> ObjectReply:
            reply = SlowReply(CONTENT)
            self.responses.append(reply)
            return reply

    downloads, objects, _ = service(SlowObjects(), timeout=0.02)
    task = asyncio.create_task(
        downloads.prepare(
            kind="saved", ident="content-demo", workspace_id="workspace-demo", range_header=None
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    await asyncio.sleep(0.04)
    assert not task.done() and downloads._active == 1
    release.set()
    with pytest.raises(DownloadError, match="download_unavailable"):
        await task
    assert downloads._active == 0
    assert objects.responses[0].closed and objects.responses[0].released


async def test_response_deadline_closes_prepared_content() -> None:
    downloads, _, _ = service()
    prepared = await downloads.prepare(
        kind="saved", ident="content-demo", workspace_id="workspace-demo", range_header=None
    )
    response = _DownloadResponse(prepared, "request-demo", timeout=0.02)

    async def send(message: Any) -> None:
        await asyncio.Event().wait()

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    with pytest.raises(TimeoutError):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert prepared.closed and downloads._active == 0
