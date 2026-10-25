"""Render-unit operations over the real ASGI runtime and disposable PostgreSQL.

These tests drive the served surface end to end: the generated request envelope, the
contract-validated response, and the typed error codes the operations declare.
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
CREATE_KEY = "render-http-key-0001"
BODY: dict[str, Any] = {
    "ordinal": 0,
    "text": "合成旁白单元",
    "voice": {"id": "voice_demo", "revision": "1"},
    "model": "tts-quality",
    "model_binding": {"id": "binding_demo", "revision": "3"},
}


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


async def chapter(client: httpx.AsyncClient) -> str:
    project = await create_project(client, "render-http-project-key", "合成有声书项目")
    book = await client.post(
        f"/v1/workbench/projects/{project}/books",
        json={"title": "合成有声书"},
        headers={"Idempotency-Key": "render-http-book-key"},
    )
    assert book.status_code == 201, book.text
    made = await client.post(
        f"/v1/workbench/books/{book.json()['id']}/chapters",
        json={"title": "第一章", "ordinal": 0, "text": "合成章节正文"},
        headers={"Idempotency-Key": "render-http-chapter-key"},
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


async def create(client: httpx.AsyncClient, chapter_id: str, **overrides: Any) -> httpx.Response:
    body = {**BODY, **overrides}
    return await client.post(
        f"/v1/workbench/chapters/{chapter_id}/render-units",
        json=body,
        headers={"Idempotency-Key": CREATE_KEY},
    )


async def test_render_unit_create_replays_and_updates_over_real_http(
    client: httpx.AsyncClient,
) -> None:
    chapter_id = await chapter(client)
    created = await create(client, chapter_id)
    assert created.status_code == 201, created.text
    unit = created.json()
    assert unit["object"] == "nexus.render_unit"
    assert unit["chapter_id"] == chapter_id
    assert (unit["revision"], unit["status"], unit["ordinal"]) == ("1", "pending", 0)
    assert unit["model_binding"] == {"id": "binding_demo", "revision": "3"}
    # No saved copy exists yet, so the operation must not invent one.
    assert unit["saved_asset_id"] is None

    replay = await create(client, chapter_id)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == unit["id"]

    fetched = await client.get(f"/v1/workbench/render-units/{unit['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == unit

    revised = await client.patch(
        f"/v1/workbench/render-units/{unit['id']}",
        json={"expected_revision": "1", "text": "改写后的旁白单元"},
    )
    assert revised.status_code == 200, revised.text
    assert (revised.json()["revision"], revised.json()["status"]) == ("2", "stale")
    assert revised.json()["text"] == "改写后的旁白单元"

    stale = await client.patch(
        f"/v1/workbench/render-units/{unit['id']}",
        json={"expected_revision": "1", "model": "tts-other"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "revision_conflict"


async def test_render_unit_errors_are_typed_and_content_free(client: httpx.AsyncClient) -> None:
    chapter_id = await chapter(client)
    missing = await client.post(
        f"/v1/workbench/chapters/{chapter_id}/render-units",
        json={key: value for key, value in BODY.items() if key != "model_binding"},
        headers={"Idempotency-Key": CREATE_KEY},
    )
    assert missing.status_code in {400, 422}, missing.text
    assert "binding_demo" not in missing.text

    unknown = await client.get("/v1/workbench/render-units/unit_absent")
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "resource_not_found"

    created = await create(client, chapter_id)
    assert created.status_code == 201, created.text
    conflict = await create(client, chapter_id, text="另一个合成旁白单元")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    # The request envelope already refuses a patch with nothing to change, so the
    # application-level refusal only guards callers that bypass HTTP.
    empty = await client.patch(
        f"/v1/workbench/render-units/{created.json()['id']}",
        json={"expected_revision": "1"},
    )
    assert empty.status_code == 400, empty.text
    assert empty.json()["error"]["code"] == "invalid_request"


async def test_render_unit_listing_pages_over_real_http(client: httpx.AsyncClient) -> None:
    chapter_id = await chapter(client)
    created = []
    for index in range(3):
        made = await client.post(
            f"/v1/workbench/chapters/{chapter_id}/render-units",
            json={**BODY, "ordinal": index},
            headers={"Idempotency-Key": f"render-list-key-{index}"},
        )
        assert made.status_code == 201, made.text
        created.append(made.json())

    first = await client.get(f"/v1/workbench/chapters/{chapter_id}/render-units?limit=2")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["object"] == "nexus.list"
    assert [item["id"] for item in body["data"]] == [created[2]["id"], created[1]["id"]]
    assert body["has_more"] is True and body["next_cursor"]

    second = await client.get(
        f"/v1/workbench/chapters/{chapter_id}/render-units",
        params={"limit": 2, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    page = second.json()
    assert [item["id"] for item in page["data"]] == [created[0]["id"]]
    assert page["has_more"] is False and page["next_cursor"] is None
    # A listed unit carries exactly what a direct read returns.
    single = await client.get(f"/v1/workbench/render-units/{created[0]['id']}")
    assert single.status_code == 200, single.text
    assert page["data"][0] == single.json()

    # The cursor stays inside its chapter, an absent chapter is missing, and an
    # out-of-range limit is refused at the boundary.
    other = await other_chapter(client)
    foreign = await client.get(
        f"/v1/workbench/chapters/{other}/render-units", params={"cursor": body["next_cursor"]}
    )
    assert foreign.status_code == 400
    assert foreign.json()["error"]["code"] == "invalid_cursor"
    absent = await client.get("/v1/workbench/chapters/chapter-absent/render-units")
    assert absent.status_code == 404
    refused = await client.get(f"/v1/workbench/chapters/{chapter_id}/render-units?limit=0")
    assert refused.status_code == 400


async def other_chapter(client: httpx.AsyncClient) -> str:
    project = await create_project(client, "render-list-project-key", "第二个有声书项目")
    book = await client.post(
        f"/v1/workbench/projects/{project}/books",
        json={"title": "第二本有声书"},
        headers={"Idempotency-Key": "render-list-book-key"},
    )
    assert book.status_code == 201, book.text
    made = await client.post(
        f"/v1/workbench/books/{book.json()['id']}/chapters",
        json={"title": "第一章", "ordinal": 0, "text": "合成章节正文"},
        headers={"Idempotency-Key": "render-list-chapter-key"},
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"])
