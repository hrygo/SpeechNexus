"""The public voice reads are registered and keep their frozen error surface."""

import httpx
import pytest
from speechnexus_contracts.voice_catalogue_http import validate_response

from speechnexus_backend.app import create_app
from tests.unit.backend.test_health import _settings


@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/v1/nexus/voices", "listVoices"),
        ("/v1/nexus/voices/voice-demo-1/revisions/2", "getVoiceRevision"),
    ],
)
async def test_unconfigured_voice_catalogue_is_retryable(path: str, operation: str) -> None:
    app = create_app(settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path, headers={"X-Request-ID": "req-voices-442"})
    assert response.status_code == 503
    assert response.headers["x-should-retry"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["retryable"] is True
    assert response.json()["error"]["request_id"] == "req-voices-442"
    validate_response(operation, 503, {"body": response.json(), "headers": dict(response.headers)})


def test_runtime_openapi_projects_only_the_two_voice_reads() -> None:
    paths = create_app(settings=_settings()).openapi()["paths"]
    assert paths["/v1/nexus/voices"]["get"]["operationId"] == "listVoices"
    fixed = paths["/v1/nexus/voices/{voice_id}/revisions/{revision}"]
    assert set(fixed) == {"get"}
    assert fixed["get"]["operationId"] == "getVoiceRevision"
    assert "/v1/nexus/voices/{voice_id}/revisions/{revision}/retirements" not in paths
