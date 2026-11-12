"""Contract-validated redacted diagnostics over the in-process readiness probes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import AnyHttpUrl, SecretStr
from speechnexus_contracts.admin_http import validate_response

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.interfaces.http.diagnostics import OPERATION, PATH
from tests.fake.readiness import StaticProbe

GENERATED_AT = datetime(2026, 11, 13, 4, 30, 0, 123456, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        service_name="speechnexus-api-test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
    )


def _client(*, fail: bool = False) -> AsyncClient:
    readiness = ReadinessService(
        [
            StaticProbe("postgresql", failure="unavailable" if fail else None),
            StaticProbe("object_storage"),
        ],
        timeout_seconds=1,
    )
    app = create_app(settings=_settings(), readiness=readiness)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def client() -> Iterator[AsyncClient]:
    client = _client()
    yield client


async def get(client: AsyncClient) -> httpx.Response:
    return await client.get(PATH, headers={"X-Request-ID": "request-diagnostics-1"})


async def test_ready_diagnostics_report_every_probe(client: AsyncClient) -> None:
    response = await get(client)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {
        "status": "ok",
        "generated_at": body["generated_at"],
        "checks": [
            {"name": "postgresql", "status": "ok", "detail": None},
            {"name": "object_storage", "status": "ok", "detail": None},
        ],
    }
    assert body["generated_at"].endswith("Z")
    validate_response(OPERATION, 200, {"body": body})


async def test_failed_probe_marks_the_snapshot_unready() -> None:
    async with _client(fail=True) as client:
        response = await get(client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unready"
    assert body["checks"][0] == {
        "name": "postgresql",
        "status": "failed",
        "detail": "unavailable",
    }
    assert body["checks"][1]["status"] == "ok"
    validate_response(OPERATION, 200, {"body": body})


async def test_the_snapshot_uses_the_injected_clock() -> None:
    from speechnexus_backend.speech.interfaces.http.diagnostics import diagnostics_body

    readiness = ReadinessService([StaticProbe("fixture")], timeout_seconds=1)
    report = await readiness.inspect()

    body = diagnostics_body(report, GENERATED_AT)

    assert body["generated_at"] == "2026-11-13T04:30:00.123456Z"
    validate_response(OPERATION, 200, {"body": body})


async def test_a_timezone_naive_clock_is_rejected() -> None:
    from speechnexus_backend.speech.application.readiness import (
        ProbeResult,
        ProbeStatus,
        ReadinessReport,
    )
    from speechnexus_backend.speech.interfaces.http.diagnostics import diagnostics_body

    report = ReadinessReport(checks=(ProbeResult("fixture", ProbeStatus.OK),))
    with pytest.raises(ValueError):
        diagnostics_body(report, datetime(2026, 11, 13))


async def test_an_inspection_failure_is_a_declared_503(monkeypatch: pytest.MonkeyPatch) -> None:
    readiness = ReadinessService([StaticProbe("fixture")], timeout_seconds=1)

    async def broken(self: ReadinessService) -> object:
        raise RuntimeError("probe plumbing failed")

    monkeypatch.setattr(ReadinessService, "inspect", broken)
    app = create_app(settings=_settings(), readiness=readiness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await get(client)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "diagnostics_unavailable"
    assert body["error"]["retryable"] is True
    validate_response(OPERATION, 503, {"body": body})
