"""Recorded generation attempts are readable without inventing a page-side protocol."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.domain.render_units import (
    RenderDependencies,
    RenderUnit,
    RevisionPin,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)
PATH = "/v2/workbench/render-units/unit_1/generations"


def started() -> RenderUnit:
    dependencies = RenderDependencies(
        original=RevisionPin("original", 1),
        working=RevisionPin("working", 2),
        chapter_id="chapter",
        role_id="narrator",
        text_plan=RevisionPin("plan", 1),
        voice=RevisionPin("voice", 3),
        model="tts-quality",
        model_binding=RevisionPin("binding", 4),
        text="合成旁白单元",
    )
    return RenderUnit.create(
        unit_id="unit_1", book_id="book", ordinal=0, dependencies=dependencies, now=NOW
    ).start(expected_revision=1, job_id="job_1", generation_id="gen:abc", now=NOW)


class _Service:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generations(self, **kw: Any) -> tuple[RenderUnit, tuple[Any, ...], bool]:
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        unit = started()
        return unit, tuple(reversed(unit.generations)), False


def app(service: _Service | None) -> FastAPI:
    return create_app(
        None,
        render_units=service,  # type: ignore[arg-type]
        default_workspace_id="workspace-demo",
        deployment_id="synthetic",
        cursor_key=b"synthetic-32-byte-cursor-key-00000",
        max_json_bytes=4096,
        body_timeout_seconds=0.02,
    )


async def call(service: _Service, *, query: str = "") -> httpx.Response:
    transport = httpx.ASGITransport(app=app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://workbench.test") as client:
        return await client.get(PATH + query)


async def test_history_reports_the_recorded_attempt_newest_first() -> None:
    service = _Service()
    response = await call(service)
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "nexus.list"
    assert body["unit_id"] == "unit_1"
    assert body["has_more"] is False
    assert body["data"] == [
        {
            "id": "gen:abc",
            "job_id": "job_1",
            "dependencies_sha256": started().dependencies.sha256,
            "revision": str(started().generations[0].revision),
        }
    ]
    assert service.calls == [{"unit_id": "unit_1", "workspace_id": "workspace-demo", "limit": 20}]


async def test_an_unknown_unit_is_not_found() -> None:
    response = await call(_Service(ResourceNotFoundError()))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


async def test_the_limit_is_forwarded_and_bounded_by_the_contract() -> None:
    service = _Service()
    assert (await call(service, query="?limit=5")).status_code == 200
    assert service.calls[-1]["limit"] == 5
    assert (await call(service, query="?limit=100")).status_code == 200
    assert service.calls[-1]["limit"] == 100
    rejected = await call(service, query="?limit=0")
    assert rejected.status_code == 400
