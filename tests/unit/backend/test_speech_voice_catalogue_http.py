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


def test_runtime_openapi_projects_the_reads_and_the_retirement_command() -> None:
    paths = create_app(settings=_settings()).openapi()["paths"]
    assert paths["/v1/nexus/voices"]["get"]["operationId"] == "listVoices"
    fixed = paths["/v1/nexus/voices/{voice_id}/revisions/{revision}"]
    assert set(fixed) == {"get"}
    assert fixed["get"]["operationId"] == "getVoiceRevision"
    retired = paths["/v1/nexus/voices/{voice_id}/revisions/{revision}/retirements"]
    assert set(retired) == {"post"}
    assert retired["post"]["operationId"] == "retireVoiceRevision"


class UnreadableCatalogue:
    def list_voices(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("An invalid request must not reach the catalogue")

    def get_revision(self, voice_id: str, revision: str) -> dict[str, object] | None:
        raise AssertionError("An invalid request must not reach the catalogue")


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/v1/nexus/voices?limit=0", "listVoices"),
        ("/v1/nexus/voices?limit=101", "listVoices"),
        ("/v1/nexus/voices?limit=bad", "listVoices"),
        ("/v1/nexus/voices?limit=1&limit=2", "listVoices"),
        ("/v1/nexus/voices?language=en&language=en", "listVoices"),
        ("/v1/nexus/voices?private_endpoint=synthetic", "listVoices"),
        ("/v1/nexus/voices/voice-demo-1/revisions/0", "getVoiceRevision"),
        ("/v1/nexus/voices/voice-demo-1/revisions/2?limit=1", "getVoiceRevision"),
    ],
)
async def test_invalid_requests_are_non_retryable_before_catalogue_access(
    path: str, operation: str, configured: bool
) -> None:
    app = create_app(settings=_settings(), voices=UnreadableCatalogue() if configured else None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path, headers={"X-Request-ID": "req-invalid-voices"})
    assert response.status_code == 400
    assert response.headers["x-should-retry"] == "false"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "voice_invalid_request"
    assert response.json()["error"]["retryable"] is False
    assert response.json()["error"]["request_id"] == "req-invalid-voices"
    assert "synthetic" not in response.text
    validate_response(operation, 400, {"body": response.json(), "headers": dict(response.headers)})
