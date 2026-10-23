"""HTTP contract tests for backend operational health routes."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from pydantic import AnyHttpUrl, SecretStr

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.readiness import ReadinessService
from tests.fake.readiness import StaticProbe


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


async def test_health_reports_liveness_and_preserves_valid_request_id() -> None:
    async with _client() as client:
        response = await client.get("/healthz", headers={"X-Request-ID": "req:test-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req:test-123"
    assert response.json() == {
        "status": "ok",
        "service": "speechnexus-api-test",
        "version": "0.1.0.dev0",
    }


async def test_health_replaces_an_invalid_request_id() -> None:
    async with _client() as client:
        response = await client.get("/healthz", headers={"X-Request-ID": "invalid id"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] != "invalid id"
    assert len(response.headers["x-request-id"]) == 32


async def test_ready_returns_dependency_states() -> None:
    async with _client() as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": [
            {"name": "postgresql", "status": "ok", "detail": None},
            {"name": "object_storage", "status": "ok", "detail": None},
        ],
    }


async def test_ready_fails_closed_when_a_dependency_fails() -> None:
    async with _client(fail=True) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unready"
    assert body["checks"][0] == {
        "name": "postgresql",
        "status": "failed",
        "detail": "unavailable",
    }


def test_settings_expose_explicit_cluster_identity() -> None:
    settings = Settings(
        service_name="speechnexus-scheduler",
        process_role="scheduler",
        node_id="node-gpu-01",
        instance_id="scheduler-1",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
    )

    assert settings.process_role == "scheduler"
    assert settings.node_id == "node-gpu-01"
    assert settings.instance_id == "scheduler-1"
