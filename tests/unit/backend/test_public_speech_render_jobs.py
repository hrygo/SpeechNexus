"""Render dispatch keeps one bounded Job create and reports an unknown receipt honestly."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from speechnexus_backend.workbench.application.render_reconcile import RenderJobReadError
from speechnexus_backend.workbench.application.render_units import (
    RenderSubmissionConflictError,
    RenderSubmissionRejectedError,
    RenderSubmissionUnknownError,
)
from speechnexus_backend.workbench.domain.render_units import (
    RenderDependencies,
    RenderUnit,
    RevisionPin,
)
from speechnexus_backend.workbench.infrastructure.public_speech_render_jobs import (
    PublicSpeechRenderJobs,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def unit(**changes: Any) -> RenderUnit:
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
        unit_id="unit", book_id="book", ordinal=0, dependencies=dependencies, now=NOW
    )


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://speech.test", transport=httpx.MockTransport(handler))


async def test_submit_sends_the_fixed_intent_and_returns_the_job_identity() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("idempotency-key")
        return httpx.Response(202, json={"id": "job_1"})

    job_id = await PublicSpeechRenderJobs(client(handler)).submit(
        unit=unit(), key="key-12345678", reference="gen:abc"
    )

    assert job_id == "job_1"
    assert seen["key"] == "key-12345678"
    assert seen["body"] == {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "合成旁白单元"},
        "voice": {"id": "voice", "revision": "3"},
        "output": {"audio_format": "wav", "save_artifacts": True},
        "client_reference": "gen:abc",
    }


async def test_keyless_submit_omits_the_header_and_keeps_the_reference() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("idempotency-key")
        seen["reference"] = json.loads(request.content)["client_reference"]
        return httpx.Response(202, json={"id": "job_2"})

    assert (
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit(), key=None, reference="gen:keyless"
        )
        == "job_2"
    )
    assert seen["key"] is None
    assert seen["reference"] == "gen:keyless"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (409, RenderSubmissionConflictError),
        (400, RenderSubmissionRejectedError),
        (401, RenderSubmissionRejectedError),
        (422, RenderSubmissionRejectedError),
    ],
)
async def test_refusals_are_not_reported_as_unknown(status: int, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    with pytest.raises(error):
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit(), key="key-12345678", reference="gen:abc"
        )


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503, json={}),
        lambda request: httpx.Response(202, json={"no": "id"}),
        lambda request: httpx.Response(202, json={"id": "bad id"}),
        lambda request: httpx.Response(202, content=b"{"),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
    ],
)
async def test_unknown_and_unreadable_receipts_never_claim_absence(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    with pytest.raises(RenderSubmissionUnknownError):
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit(), key="key-12345678", reference="gen:abc"
        )


async def test_an_unrepresentable_unit_never_opens_a_connection() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202, json={"id": "job_1"})

    with pytest.raises(RenderSubmissionRejectedError):
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit(), key=None, reference="gen:" + "x" * 300
        )
    assert calls == 0


def job(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": "job_1",
        "object": "nexus.job",
        "kind": "tts",
        "status": "succeeded",
        "cleanup_state": "not_required",
        "request_id": "req_1",
        "workspace_id": "workspace-demo",
        "execution_revision": "1",
        "progress": {"unit": "audio_ms", "completed": 1, "total": 1},
        "artifacts": [
            {
                "artifact_id": "artifact_1",
                "kind": "audio",
                "media_type": "audio/wav",
                "bytes": 1024,
                "sha256": "sha256:" + "a" * 64,
                "receipt_id": "receipt_1",
            }
        ],
        "result": {
            "kind": "tts",
            "audio_artifact_id": "artifact_1",
            "receipt_id": "receipt_1",
        },
        "operations": [],
        "effective_configuration": {
            "models": {"tts": {"id": "tts-quality", "revision": "1"}},
            "policy_revision": "1",
            "catalog_revision": "1",
        },
        "error": None,
        "created_at": "2026-10-24T00:00:00Z",
        "updated_at": "2026-10-24T00:00:01Z",
    }
    value.update(changes)
    return value


async def test_read_job_returns_the_tts_receipt_of_a_succeeded_job() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=job())

    async with client(handler) as http:
        outcome = await PublicSpeechRenderJobs(http).read_job(job_id="job_1")
    assert paths == ["/v1/nexus/jobs/job_1"]
    assert (outcome.state, outcome.artifact_id, outcome.receipt_id) == (
        "succeeded",
        "artifact_1",
        "receipt_1",
    )


async def test_read_job_reports_terminal_failures_and_pending_states() -> None:
    async with client(
        lambda _: httpx.Response(200, json=job(status="running", result=None))
    ) as http:
        assert (await PublicSpeechRenderJobs(http).read_job(job_id="job_1")).state == "pending"
    failed = job(
        status="failed",
        result=None,
        artifacts=[],
        cleanup_state="complete",
        error={"code": "engine_failure", "message": "synthetic", "retryable": False},
    )
    async with client(lambda _: httpx.Response(200, json=failed)) as http:
        assert (await PublicSpeechRenderJobs(http).read_job(job_id="job_1")).state == "failed"
    expired = job(status="expired", result=None, artifacts=[], cleanup_state="not_required")
    async with client(lambda _: httpx.Response(200, json=expired)) as http:
        assert (await PublicSpeechRenderJobs(http).read_job(job_id="job_1")).state == "expired"


async def test_read_job_refuses_an_unreadable_or_foreign_receipt() -> None:
    with pytest.raises(RenderJobReadError):
        async with client(lambda _: httpx.Response(200, json=job(kind="asr"))) as http:
            await PublicSpeechRenderJobs(http).read_job(job_id="job_1")
    with pytest.raises(RenderJobReadError):
        async with client(lambda _: httpx.Response(503, json={"error": {}})) as http:
            await PublicSpeechRenderJobs(http).read_job(job_id="job_1")

    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=job())

    with pytest.raises(RenderJobReadError):
        async with client(handler) as http:
            await PublicSpeechRenderJobs(http).read_job(job_id="job_1/../other")
    assert called is False
