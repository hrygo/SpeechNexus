"""Generation ingress returns the accepted attempt and never hides an unknown result."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.render_units import (
    RenderDispatchUnavailableError,
    RenderSubmissionUnknownError,
)
from speechnexus_backend.workbench.domain.render_units import (
    RenderDependencies,
    RenderUnit,
    RevisionPin,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)
PATH = "/v2/workbench/render-units/unit_1/generations"


def unit() -> RenderUnit:
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

    async def start_unit(self, **kw: Any) -> tuple[RenderUnit, str]:
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        return unit(), "job_1"


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


async def call(
    service: _Service | None, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://workbench.test") as client:
        return await client.post(PATH, json={"expected_unit_revision": "1"}, headers=headers or {})


async def test_generation_returns_the_recorded_attempt() -> None:
    service = _Service()
    response = await call(service, headers={"Idempotency-Key": "key-12345678"})
    assert response.status_code == 202
    body = response.json()
    assert body["speech_job_id"] == "job_1"
    assert body["render_unit"]["status"] == "generating"
    assert body["render_unit"]["speech_job_id"] == "job_1"
    assert service.calls == [
        {
            "unit_id": "unit_1",
            "workspace_id": "workspace-demo",
            "expected_revision": 1,
            "key": "key-12345678",
        }
    ]


async def test_an_unknown_submission_is_reported_as_unknown_not_absent() -> None:
    response = await call(_Service(RenderSubmissionUnknownError()))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "outcome_unknown"


async def test_without_a_producer_the_route_reports_unavailable() -> None:
    response = await call(_Service(RenderDispatchUnavailableError()))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "workbench_unavailable"


async def test_v1_generation_is_not_declared_here() -> None:
    transport = httpx.ASGITransport(app=app(_Service()))
    async with httpx.AsyncClient(transport=transport, base_url="http://workbench.test") as client:
        response = await client.post(
            "/v1/workbench/render-units/unit_1/generations",
            json={"expected_unit_revision": "1"},
        )
    assert response.status_code in {404, 405}
