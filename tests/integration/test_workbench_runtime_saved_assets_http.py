"""The saved-copy read path over real HTTP against the real runtime and PostgreSQL.

The workbench's own copy is written by `saveProjectAsset` and read back by id through
`getSavedAsset`. These cases pin the wire behaviour the production client depends on: the
copy is created from a real artifact served by the base, reading it back returns the
workbench's own record without asking the base again, an unknown id is a stable 404, and a
deployment without an object zone refuses instead of pretending the copy exists.

Opt-in: `--workbench-runtime` with the disposable PostgreSQL and the isolated MinIO.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from minio import Minio
from pydantic import SecretStr
from tests.integration.test_workbench_runtime import Database, serve
from tests.integration.test_workbench_runtime import database as database

from speechnexus_backend.workbench.runtime import create_runtime_app

pytestmark = pytest.mark.integration

WORKSPACE = "workspace-saved-read"
BUCKET = "workbench-saved-read"
ARTIFACT = "artifact-saved-read"
RECEIPT = "receipt-saved-read"
CONTENT = b"synthetic saved copy\n"
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()


@dataclass
class _Base:
    """A loopback stand-in for the speech base: artifact metadata plus its bytes."""

    reads: int = 0
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            method, path, _ = headers.split(b"\r\n", 1)[0].split(b" ", 2)
            assert method == b"GET"
            self.reads += 1
            if path == f"/v1/nexus/artifacts/{ARTIFACT}".encode():
                body = json.dumps(
                    {
                        "id": ARTIFACT,
                        "object": "nexus.artifact",
                        "workspace_id": WORKSPACE,
                        "kind": "audio",
                        "status": "ready",
                        "media_type": "audio/wav",
                        "bytes": len(CONTENT),
                        "sha256": DIGEST,
                        "receipt_id": RECEIPT,
                        "created_at": "2026-10-25T00:00:00Z",
                    }
                ).encode()
                self._write(writer, b"application/json", body)
            else:
                assert path == f"/v1/nexus/artifacts/{ARTIFACT}/content".encode()
                self._write(writer, b"application/octet-stream", CONTENT)
            await writer.drain()
        except ConnectionError, asyncio.CancelledError:
            pass
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            finally:
                self.tasks.discard(task)

    def _write(self, writer: asyncio.StreamWriter, media: bytes, body: bytes) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: "
            + media
            + b"\r\nConnection: close\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )


@pytest.fixture
def objects() -> Iterator[None]:
    assert os.environ.get("SPEECHNEXUS_TEST_MINIO_ENDPOINT"), (
        "isolated MinIO service is required for object evidence"
    )
    yield None


def runtime_settings(database: Database, base_origin: str, *, objects: bool) -> Any:
    update: dict[str, Any] = {"workspace_id": WORKSPACE, "speech_base_url": base_origin}
    if objects:
        endpoint = os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"]
        client = Minio(
            endpoint.replace("http://", "").replace("https://", ""),
            access_key=os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"],
            secret_key=os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"],
            secure=endpoint.startswith("https://"),
        )
        if not client.bucket_exists(BUCKET):
            client.make_bucket(BUCKET)
        update |= {
            "object_endpoint": endpoint,
            "object_bucket": BUCKET,
            "object_prefix": "saved",
            "object_access_key": SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"]),
            "object_secret_key": SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"]),
        }
    return database.settings().model_copy(update=update)


async def test_a_copy_written_from_the_base_is_read_back_over_http_without_refetching(
    database: Database, objects: None
) -> None:
    state = _Base()
    server = await asyncio.start_server(state.connection, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        settings = runtime_settings(database, f"http://{host}:{port}", objects=True)
        async with serve(create_runtime_app(settings)) as client:
            project = await client.post("/v1/workbench/projects", json={"name": "合成副本项目"})
            assert project.status_code == 201, project.text
            created = await client.post(
                f"/v1/workbench/projects/{project.json()['id']}/saved-assets",
                json={
                    "source_artifact_id": ARTIFACT,
                    "source_receipt_id": RECEIPT,
                    "display_name": "合成章节",
                },
            )
            assert created.status_code == 201, created.text
            saved = created.json()
            assert saved["object"] == "nexus.saved_asset"
            assert saved["sha256"] == DIGEST and saved["bytes"] == len(CONTENT)
            assert saved["source_artifact_id"] == ARTIFACT
            reads_after_save = state.reads
            # The source is now offline: a committed copy must still be independently readable.
            server.close()
            await server.wait_closed()

            read = await client.get(f"/v1/workbench/saved-assets/{saved['id']}")
            assert read.status_code == 200, read.text
            # The read-back is the workbench's own record: the base is not asked again.
            assert read.json() == saved
            assert state.reads == reads_after_save

            path = f"/v1/workbench/saved-assets/{saved['id']}/content"
            content = await client.get(path)
            assert content.status_code == 200, content.text
            assert content.content == CONTENT
            assert content.headers["etag"] == '"' + DIGEST + '"'
            assert content.headers["content-length"] == str(len(CONTENT))
            for header, expected in [("bytes=0-3", CONTENT[:4]), ("bytes=-3", CONTENT[-3:])]:
                partial = await client.get(path, headers={"Range": header})
                assert partial.status_code == 206 and partial.content == expected
            outside = await client.get(path, headers={"Range": f"bytes={len(CONTENT)}-"})
            assert outside.status_code == 416
            assert outside.headers["content-range"] == f"bytes */{len(CONTENT)}"
            assert state.reads == reads_after_save

            missing = await client.get("/v1/workbench/saved-assets/asset_saved_unknown")
            assert missing.status_code == 404, missing.text
            assert missing.json()["error"]["code"] == "resource_not_found"
    finally:
        server.close()
        await server.wait_closed()
        if state.tasks:
            await asyncio.wait_for(asyncio.gather(*state.tasks), 2)
        assert not state.errors


async def test_a_deployment_without_an_object_zone_serves_no_copy_read_path(
    database: Database,
) -> None:
    """No object zone, no saved-copy route: the deployment answers not-found, never a record.

    The client maps this to "该已保存副本已不可读取", which is the honest reading of a route
    this deployment does not serve at all.
    """
    async with serve(
        create_runtime_app(runtime_settings(database, "http://127.0.0.1:59998", objects=False))
    ) as client:
        read = await client.get("/v1/workbench/saved-assets/asset_saved_any")
    assert read.status_code == 404, read.text
    assert read.json()["error"]["code"] == "resource_not_found"
