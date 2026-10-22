"""Operational contract tests for the execution gateway."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.readiness import ReadinessService
from speechnexus_gateway.settings import Settings
from tests.fake.readiness import StaticProbe


def _client(*, fail: bool = False) -> AsyncClient:
    settings = Settings(
        service_name="speechnexus-gateway-test",
        control_database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
    )
    readiness = ReadinessService(
        [StaticProbe("control_postgresql", failure="unavailable" if fail else None)],
        timeout_seconds=1,
    )
    app = create_app(settings=settings, readiness=readiness)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_gateway_liveness_and_readiness() -> None:
    async with _client() as client:
        health = await client.get("/healthz")
        readiness = await client.get("/readyz")

    assert health.status_code == 200
    assert health.json()["service"] == "speechnexus-gateway-test"
    assert readiness.status_code == 200
    assert readiness.json()["checks"] == [
        {"name": "control_postgresql", "status": "ok", "detail": None}
    ]


async def test_gateway_readiness_fails_closed() -> None:
    async with _client(fail=True) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "unready"


def test_gateway_settings_expose_node_and_gateway_identity() -> None:
    settings = Settings(
        service_name="speechnexus-gateway-test",
        node_id="node-gpu-01",
        gateway_id="gateway-node-01",
        control_database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
    )

    assert settings.node_id == "node-gpu-01"
    assert settings.gateway_id == "gateway-node-01"
