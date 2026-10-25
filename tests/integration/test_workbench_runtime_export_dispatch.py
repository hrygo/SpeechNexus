"""The serving runtime owns export production: claim, produce, publish, settle.

The producer is a test-only saved-audio assembler using decodable synthetic audio.
It invokes no inference. The queue, claim, validator, publication and dispatch loop
are production paths over disposable PostgreSQL and the real object zone.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from minio import Minio
from pydantic import SecretStr
from tests.fake.export_media import synthetic_export_zip
from tests.integration.test_workbench_exports_http import create, prepared
from tests.integration.test_workbench_runtime import Database, serve
from tests.integration.test_workbench_runtime import database as database

from speechnexus_backend.workbench.application.export_worker import ProducedExport
from speechnexus_backend.workbench.infrastructure.minio_object_zone import MinioObjectZone
from speechnexus_backend.workbench.runtime import create_runtime_app

pytestmark = pytest.mark.integration
CONTENT = synthetic_export_zip(chapters=1)
BUCKET = "speechnexus-workbench-runtime-exports"
DEADLINE = 20.0


class _Bundle:
    """Test-only producer: reports its one item, then returns synthetic bytes."""

    async def produce_export(self, job: Any, *, report: Any) -> ProducedExport:
        await report(1)
        return ProducedExport(content=CONTENT, media_type="application/zip")


class _Zone:
    def __init__(self, zone: MinioObjectZone, client: Minio) -> None:
        self.zone = zone
        self.client = client

    def read(self, key: str) -> bytes:
        response = self.client.get_object(BUCKET, f"exports/{key}")
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()


@pytest.fixture
def objects() -> Iterator[_Zone]:
    endpoint = os.environ.get("SPEECHNEXUS_TEST_MINIO_ENDPOINT")
    access = os.environ.get("SPEECHNEXUS_TEST_MINIO_ACCESS_KEY")
    secret = os.environ.get("SPEECHNEXUS_TEST_MINIO_SECRET_KEY")
    assert endpoint and access and secret, "isolated MinIO service is required for object evidence"
    client = Minio(
        endpoint.replace("http://", "").replace("https://", ""),
        access_key=access,
        secret_key=secret,
        secure=endpoint.startswith("https://"),
    )
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)
    zone = MinioObjectZone(endpoint, BUCKET, access, secret, prefix="exports")
    try:
        yield _Zone(zone, client)
    finally:
        zone.close()


def runtime_app(
    database: Database,
    *,
    interval: float,
    producer: Any | None,
    max_output_bytes: int = 256 * 1024**2,
) -> Any:
    endpoint = os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"]
    settings = database.settings().model_copy(
        update={
            "export_interval_seconds": interval,
            "export_max_output_bytes": max_output_bytes,
            "object_endpoint": endpoint,
            "object_bucket": BUCKET,
            "object_prefix": "exports",
            "object_access_key": SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"]),
            "object_secret_key": SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"]),
        }
    )
    return create_runtime_app(settings, export_producer=producer)


async def queue_one(client: httpx.AsyncClient, suffix: str) -> tuple[dict[str, Any], str]:
    book_id, unit_id, casting = await prepared(client, suffix=suffix)
    created = await create(
        client,
        book_id,
        casting,
        unit_ids=[unit_id],
        key=f"runtime-export-key-{suffix}",
    )
    assert created.status_code == 202, created.text
    return created.json(), book_id


async def settled(client: httpx.AsyncClient, export_id: str, status: str) -> dict[str, Any]:
    async with asyncio.timeout(DEADLINE):
        while True:
            fetched = await client.get(f"/v1/workbench/exports/{export_id}")
            assert fetched.status_code == 200, fetched.text
            body: dict[str, Any] = fetched.json()
            if body["status"] == status:
                return body
            await asyncio.sleep(0.05)


async def test_the_serving_runtime_produces_a_queued_export(
    database: Database, objects: _Zone
) -> None:
    app = runtime_app(database, interval=0.2, producer=_Bundle())
    async with serve(app) as client:
        export, book_id = await queue_one(client, "runtime-produce")
        owned_book = await client.get(f"/v1/workbench/books/{book_id}")
        assert owned_book.status_code == 200, owned_book.text
        project_id = owned_book.json()["project_id"]
        assert export["status"] == "queued" and export["artifact"] is None
        # No test calls the worker: the serving runtime's own loop must settle it.
        finished = await settled(client, export["id"], "succeeded")
        path = f"/v1/workbench/exports/{export['id']}/content"
        downloaded = await client.get(path)
        assert downloaded.status_code == 200 and downloaded.content == CONTENT
        assert downloaded.headers["etag"] == '"sha256:' + hashlib.sha256(CONTENT).hexdigest() + '"'
        partial = await client.get(path, headers={"Range": "bytes=1-4"})
        assert partial.status_code == 206 and partial.content == CONTENT[1:5]
        assert partial.headers["content-range"] == f"bytes 1-4/{len(CONTENT)}"

    assert finished["progress_completed"] == finished["progress_total"] == 1
    artifact = finished["artifact"]
    assert artifact is not None
    assert artifact["media_type"] == "application/zip"
    assert artifact["bytes"] == len(CONTENT)
    assert artifact["sha256"] == "sha256:" + hashlib.sha256(CONTENT).hexdigest()
    assert finished["error"] is None
    assert objects.read(f"{project_id}/exports/{export['id']}/{artifact['id']}") == CONTENT


@pytest.mark.parametrize(
    ("content", "max_output_bytes"),
    [(b"synthetic invalid archive", 1_000_000), (CONTENT, len(CONTENT) - 1)],
    ids=["invalid-media", "configured-output-limit"],
)
async def test_the_runtime_rejects_invalid_or_over_budget_media_before_publication(
    database: Database, objects: _Zone, content: bytes, max_output_bytes: int
) -> None:
    class InvalidBundle(_Bundle):
        async def produce_export(self, job: Any, *, report: Any) -> ProducedExport:
            await report(1)
            return ProducedExport(content=content, media_type="application/zip")

    app = runtime_app(
        database, interval=0.2, producer=InvalidBundle(), max_output_bytes=max_output_bytes
    )
    async with serve(app) as client:
        export, book_id = await queue_one(client, "runtime-invalid")
        owned_book = await client.get(f"/v1/workbench/books/{book_id}")
        assert owned_book.status_code == 200
        project_id = owned_book.json()["project_id"]
        failed = await settled(client, export["id"], "failed")
    assert failed["artifact"] is None and failed["error"]["code"] == "unavailable"
    assert failed["progress_completed"] == failed["progress_total"] == 1
    assert (
        list(objects.client.list_objects(BUCKET, prefix=f"exports/{project_id}/", recursive=True))
        == []
    )


async def test_an_unconfigured_runtime_leaves_queued_exports_alone(
    database: Database, objects: _Zone
) -> None:
    app = runtime_app(database, interval=0, producer=_Bundle())
    async with serve(app) as client:
        export, _ = await queue_one(client, "runtime-off")
        await asyncio.sleep(1.5)
        fetched = await client.get(f"/v1/workbench/exports/{export['id']}")
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert (body["status"], body["artifact"], body["error"]) == ("queued", None, None)


async def test_a_runtime_without_a_producer_does_not_claim_work(
    database: Database, objects: _Zone
) -> None:
    # The queue and the publish path exist, but nothing may be claimed that cannot finish.
    app = runtime_app(database, interval=0.2, producer=None)
    async with serve(app) as client:
        export, _ = await queue_one(client, "runtime-no-producer")
        await asyncio.sleep(1.5)
        fetched = await client.get(f"/v1/workbench/exports/{export['id']}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["status"] == "queued"
