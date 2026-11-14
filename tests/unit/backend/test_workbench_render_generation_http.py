"""Generation ingress returns the accepted attempt and never hides an unknown result."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.render_units import (
    RenderDispatchUnavailableError,
    RenderRejection,
    RenderSubmissionRejectedError,
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


async def test_an_over_bound_unit_is_refused_instead_of_shortened() -> None:
    """A unit above the one-synthesis bound cannot be dispatched as-is.

    The pinned intent is refused and the response carries no accepted attempt, so a
    caller can never mistake a truncated synthesis for the unit's text. A locally
    proven text bound is named, not folded into the unnamed fallback.
    """
    rejected = RenderSubmissionRejectedError(RenderRejection.local_contract(param="input.text"))
    response = await call(_Service(rejected))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "render_submission_text_too_long"
    assert response.json()["error"]["param"] == "input.text"


@pytest.mark.parametrize(
    ("rejection", "code", "param"),
    [
        # One upstream status class, six different operator steps: none may absorb another.
        (
            RenderRejection.upstream(
                status_code=422, code="unsupported_parameter", param="input.text"
            ),
            "render_submission_text_too_long",
            "input.text",
        ),
        (
            RenderRejection.upstream(status_code=422, code="unsupported_parameter", param="model"),
            "render_submission_field_unsupported",
            "model",
        ),
        (
            RenderRejection.upstream(status_code=422, code="workspace_not_configured", param=None),
            "render_submission_workspace_not_configured",
            None,
        ),
        (
            RenderRejection.upstream(status_code=422, code="workspace_not_found", param=None),
            "render_submission_workspace_unavailable",
            None,
        ),
        (
            RenderRejection.upstream(status_code=422, code="workspace_disabled", param=None),
            "render_submission_workspace_unavailable",
            None,
        ),
        (
            RenderRejection.upstream(
                status_code=422, code="voice_not_published", param="voice.revision"
            ),
            "render_submission_voice_unavailable",
            "voice.revision",
        ),
        (
            RenderRejection.upstream(
                status_code=422, code="asset_not_ready", param="input.asset_id"
            ),
            "render_submission_asset_unavailable",
            "input.asset_id",
        ),
        (
            RenderRejection.upstream(status_code=413, code="job_request_too_large", param=None),
            "render_submission_body_too_large",
            None,
        ),
    ],
)
async def test_a_refusal_reports_the_operator_step_it_demands(
    rejection: RenderRejection, code: str, param: str | None
) -> None:
    """The public code names what the operator must do, and `param` stays a field pointer."""
    response = await call(_Service(RenderSubmissionRejectedError(rejection)))
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["param"] == param
    assert error["retryable"] is False


@pytest.mark.parametrize(
    "rejection",
    [
        # A local refusal whose proven cause is not the text bound.
        RenderRejection.local_contract(),
        # An upstream refusal whose envelope could not be read: no cause may be invented.
        RenderRejection.unreadable(status_code=422),
        # An upstream code whose operator step this vocabulary does not name.
        RenderRejection.upstream(status_code=422, code="model_binding_not_published", param=None),
        # No rejection detail at all (a producer that states nothing, and the default).
        None,
    ],
)
async def test_only_a_stated_cause_moves_the_code_off_the_fallback(
    rejection: RenderRejection | None,
) -> None:
    """`render_submission_rejected` means "no cause"; an unreadable refusal stays there."""
    response = await call(_Service(RenderSubmissionRejectedError(rejection)))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "render_submission_rejected"
    assert response.json()["error"]["param"] is None


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
