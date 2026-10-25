"""Export operations over the real ASGI runtime and disposable PostgreSQL.

These tests drive the served surface end to end: the generated envelope, the
contract-validated response and the typed error codes the operations declare.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from tests.integration.test_workbench_runtime import Database, create_project, serve
from tests.integration.test_workbench_runtime import database as database

pytestmark = pytest.mark.integration
CREATE_KEY = "export-http-key-0001"


@pytest.fixture
async def client(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    from speechnexus_backend.workbench.runtime import create_app_from_env

    for key, value in database.settings().model_dump().items():
        if value is None:
            # An unset optional service stays unconfigured instead of becoming "None".
            continue
        plain = (
            value.get_secret_value()
            if hasattr(value, "get_secret_value")
            else json.dumps(value)
            if isinstance(value, tuple)
            else str(value)
        )
        monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_" + key.upper(), plain)
    async with serve(create_app_from_env()) as client:
        yield client


async def prepared(client: httpx.AsyncClient, *, suffix: str = "one") -> tuple[str, str, str]:
    """A project, one book with one chapter and one render unit, over real HTTP."""
    project = await create_project(client, f"export-http-project-key-{suffix}", "合成导出项目")
    book = await client.post(
        f"/v1/workbench/projects/{project}/books",
        json={"title": f"合成有声书 {suffix}"},
        headers={"Idempotency-Key": f"export-http-book-key-{suffix}"},
    )
    assert book.status_code == 201, book.text
    book_id = str(book.json()["id"])
    chapter = await client.post(
        f"/v1/workbench/books/{book_id}/chapters",
        json={"title": "第一章", "ordinal": 0, "text": "合成章节正文"},
        headers={"Idempotency-Key": f"export-http-chapter-key-{suffix}"},
    )
    assert chapter.status_code == 201, chapter.text
    unit = await client.post(
        f"/v2/workbench/chapters/{chapter.json()['id']}/render-units",
        json={
            "ordinal": 0,
            "text": "合成旁白单元",
            "voice": {"id": "voice_demo", "revision": "1"},
            "model": "tts-quality",
            "model_binding": {"id": "binding_demo", "revision": "3"},
        },
        headers={"Idempotency-Key": f"export-http-unit-key-{suffix}"},
    )
    assert unit.status_code == 201, unit.text
    return book_id, str(unit.json()["id"]), str(book.json()["casting_revision"])


async def create(
    client: httpx.AsyncClient, book_id: str, casting: str, **overrides: Any
) -> httpx.Response:
    key = str(overrides.pop("key", CREATE_KEY))
    body: dict[str, Any] = {"expected_casting_revision": casting, "format": "zip"}
    body.update(overrides)
    return await client.post(
        f"/v1/workbench/books/{book_id}/exports",
        json=body,
        headers={"Idempotency-Key": key},
    )


async def test_export_create_get_and_repeated_cancellation_over_real_http(
    client: httpx.AsyncClient,
) -> None:
    book_id, unit_id, casting = await prepared(client)

    created = await create(client, book_id, casting, unit_ids=[unit_id])
    assert created.status_code == 202, created.text
    export = created.json()
    assert export["object"] == "nexus.export_job"
    assert export["book_id"] == book_id
    assert (export["status"], export["format"]) == ("queued", "zip")
    assert export["manifest_revision"] == casting
    assert (export["progress_completed"], export["progress_total"]) == (0, 1)
    # Nothing is produced yet, so the operation must not invent an artifact.
    assert export["artifact"] is None and export["error"] is None

    replay = await create(client, book_id, casting, unit_ids=[unit_id])
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == export["id"]

    fetched = await client.get(f"/v1/workbench/exports/{export['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == export

    cancelled = await client.post(
        f"/v1/workbench/exports/{export['id']}/cancellations",
        headers={"Idempotency-Key": "export-http-cancel-key-0001"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelling"

    # Repeating the cancellation changes nothing and says so with 202.
    repeated = await client.post(
        f"/v1/workbench/exports/{export['id']}/cancellations",
        headers={"Idempotency-Key": "export-http-cancel-key-0002"},
    )
    assert repeated.status_code == 202, repeated.text
    assert repeated.json() == cancelled.json()


async def test_export_errors_are_typed_and_content_free(client: httpx.AsyncClient) -> None:
    book_id, _, casting = await prepared(client)

    stale = await create(client, book_id, "9", key="export-http-stale-key")
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "revision_conflict"

    absent = await create(
        client, book_id, casting, unit_ids=["unit_absent"], key="export-http-absent-key"
    )
    assert absent.status_code == 404, absent.text
    assert absent.json()["error"]["code"] == "resource_not_found"

    malformed = await client.post(
        f"/v1/workbench/books/{book_id}/exports",
        json={"expected_casting_revision": casting},
        headers={"Idempotency-Key": "export-http-malformed-key"},
    )
    assert malformed.status_code in {400, 422}, malformed.text
    assert "unit_absent" not in malformed.text

    unknown = await client.get("/v1/workbench/exports/export_absent")
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "resource_not_found"

    cancel = await client.post(
        "/v1/workbench/exports/export_absent/cancellations",
        headers={"Idempotency-Key": "export-http-cancel-missing-key"},
    )
    assert cancel.status_code == 404, cancel.text
    assert cancel.json()["error"]["code"] == "resource_not_found"
