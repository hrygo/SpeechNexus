"""HTTP contract tests for the runtime version identity endpoint (#853)."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from pydantic import AnyHttpUrl, SecretStr
from speechnexus_contracts.contract_digest import CONTRACT_DIGEST

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.interfaces.http.version_identity import PATH
from tests.fake.readiness import StaticProbe


def _client() -> AsyncClient:
    settings = Settings(
        service_name="speechnexus-api-test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
    )
    readiness = ReadinessService(
        [StaticProbe("postgresql"), StaticProbe("object_storage")], timeout_seconds=1
    )
    app = create_app(settings=settings, readiness=readiness)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_version_identity_reports_the_generated_contract_digest() -> None:
    async with _client() as client:
        response = await client.get(PATH, headers={"X-Request-ID": "req:version-853"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req:version-853"
    # A build fact must never be cached: a stale answer would defeat the check.
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {"version": "0.1.0.dev0", "contract_digest": CONTRACT_DIGEST}
    assert CONTRACT_DIGEST.startswith("sha256:") and len(CONTRACT_DIGEST) == len("sha256:") + 64


async def test_version_identity_requires_no_configured_dependency() -> None:
    # The identity is a build fact, not a probe: an API without database or
    # object storage still answers it, which is what lets an operator compare a
    # deployment that is only half configured.
    settings = Settings(
        service_name="speechnexus-api-test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
    )
    app = create_app(
        settings=settings,
        readiness=ReadinessService(
            [StaticProbe("postgresql", failure="unavailable")], timeout_seconds=1
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unready = await client.get("/readyz")
        identity = await client.get(PATH)

    assert unready.status_code == 503
    assert identity.status_code == 200


async def test_version_identity_answers_the_public_405_envelope() -> None:
    async with _client() as client:
        response = await client.post(PATH)

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    body = response.json()
    assert body["error"]["code"] == "method_not_allowed"
    assert body["error"]["retryable"] is False
