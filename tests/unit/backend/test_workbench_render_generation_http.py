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
    WorkbenchRenderUnits,
)
from speechnexus_backend.workbench.domain.render_units import (
    RenderDependencies,
    RenderUnit,
    RevisionPin,
)
from speechnexus_backend.workbench.infrastructure.public_speech_render_jobs import (
    PublicSpeechRenderJobs,
)
from tests.unit.backend.test_public_speech_render_jobs import upstream_error
from tests.unit.backend.test_workbench_render_dispatch import _Store as DispatchStore

NOW = datetime(2026, 10, 24, tzinfo=UTC)
PATH = "/v2/workbench/render-units/unit_1/generations"


def unit(text: str = "合成旁白单元") -> RenderUnit:
    dependencies = RenderDependencies(
        original=RevisionPin("original", 1),
        working=RevisionPin("working", 2),
        chapter_id="chapter",
        role_id="narrator",
        text_plan=RevisionPin("plan", 1),
        voice=RevisionPin("voice", 3),
        model="tts-quality",
        model_binding=RevisionPin("binding", 4),
        text=text,
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


async def call(service: Any, *, headers: dict[str, str] | None = None) -> httpx.Response:
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
            # The upstream Job body carries the text as `input.text` in v1 and `text`
            # in the inline v2 shape; both name the same field the operator must split.
            RenderRejection.upstream(status_code=422, code="unsupported_parameter", param="text"),
            "render_submission_text_too_long",
            "text",
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
        RenderRejection.upstream(status_code=422, code="job_kind_not_supported", param=None),
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


def speech_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://speech.test", transport=handler)


def dispatchable(text: str = "合成旁白单元") -> RenderUnit:
    """A unit still at revision 1, so the route's `expected_unit_revision: 1` matches."""
    dependencies = RenderDependencies(
        original=RevisionPin("original", 1),
        working=RevisionPin("working", 2),
        chapter_id="chapter",
        role_id="narrator",
        text_plan=RevisionPin("plan", 1),
        voice=RevisionPin("voice", 3),
        model="tts-quality",
        model_binding=RevisionPin("binding", 4),
        text=text,
    )
    return RenderUnit.create(
        unit_id="unit_1", book_id="book", ordinal=0, dependencies=dependencies, now=NOW
    )


def dispatched(unit_record: RenderUnit, handler: httpx.MockTransport) -> WorkbenchRenderUnits:
    """The real chain: route -> real service -> real adapter -> a synthetic refusal."""
    jobs = PublicSpeechRenderJobs(speech_client(handler))
    return WorkbenchRenderUnits(DispatchStore(unit_record), jobs)


@pytest.mark.parametrize(
    ("status", "code", "param", "expected"),
    [
        (422, "unsupported_parameter", "input.text", "render_submission_text_too_long"),
        (422, "unsupported_parameter", "model", "render_submission_field_unsupported"),
        (422, "workspace_not_configured", None, "render_submission_workspace_not_configured"),
        (422, "workspace_not_found", None, "render_submission_workspace_unavailable"),
        (422, "voice_not_published", "voice.revision", "render_submission_voice_unavailable"),
        (422, "asset_not_ready", None, "render_submission_asset_unavailable"),
        (
            422,
            "model_binding_not_published",
            "model_binding.revision",
            "render_submission_binding_unavailable",
        ),
        (422, "model_binding_retired", None, "render_submission_binding_unavailable"),
        (413, "job_request_too_large", None, "render_submission_body_too_large"),
        # A refused unit is still refused; an unrecognized cause keeps the fallback.
        (422, "job_kind_not_supported", None, "render_submission_rejected"),
        # #750 / #745: upstream names no parameter today (it sends `param: null` for
        # every error), so an unsupported parameter states no cause and must stay the
        # fallback - the panel's own measurement is then the only admissible
        # attribution. Naming a field here would blame the pinned voice for an
        # over-bound text.
        (422, "unsupported_parameter", None, "render_submission_rejected"),
    ],
)
async def test_a_coded_refusal_reaches_the_route_as_a_workbench_code(
    status: int, code: str, param: str | None, expected: str
) -> None:
    """One chain, not two halves: HTTP route -> service -> adapter -> public code.

    The adapter and the mapping each have their own tests, but only this one proves
    they are wired to each other: the body below is what a client actually receives
    after the upstream refusal, so a mapping that stops being reachable (or a route
    that stops calling it) shows up here and nowhere else.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=upstream_error(code, param=param))

    response = await call(dispatched(dispatchable(), httpx.MockTransport(handler)))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == expected
    assert response.json()["error"]["retryable"] is False


async def test_an_unreadable_upstream_refusal_stays_the_unnamed_fallback() -> None:
    """A proxy page proves the refusal, not its cause: the route must not invent one."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(413, content=b"<html>413 Request Entity Too Large</html>")

    response = await call(dispatched(dispatchable(), httpx.MockTransport(handler)))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "render_submission_rejected"
    assert response.json()["error"]["param"] is None


async def test_a_locally_over_bound_unit_is_named_by_the_route_with_no_request() -> None:
    """The over-bound text never leaves the process, and the route still names it."""

    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202, json={"id": "job_1"})

    response = await call(dispatched(dispatchable("中" * 4097), httpx.MockTransport(handler)))
    assert calls == 0
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "render_submission_text_too_long"
    assert response.json()["error"]["param"] == "input.text"
