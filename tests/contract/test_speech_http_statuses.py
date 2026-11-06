"""Previously undeclared statuses match real HTTP replies and the public schema."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from jsonschema import ValidationError

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.assets import AssetService
from tests.support.contracts import validator
from tests.unit.backend.test_artifact_http import ARTIFACT_ID, CONTENT, service
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_public_jobs import _service as job_service
from tests.unit.backend.test_transcription_http import chain, files

CASES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures/contracts/speech-http-statuses.json"
    ).read_text()
)
SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)


def response_contract(operation: str, status: int) -> dict[str, Any]:
    for item in DOCUMENT["paths"].values():
        for specification in item.values():
            if isinstance(specification, dict) and specification.get("operationId") == operation:
                response: dict[str, Any] = specification["responses"][str(status)]
                while "$ref" in response:
                    response = DOCUMENT["components"]["responses"][
                        response["$ref"].rsplit("/", 1)[1]
                    ]
                return response
    raise AssertionError(operation)


def assert_error_contract(operation: str, response: httpx.Response) -> None:
    contract = response_contract(operation, response.status_code)
    schema = contract["content"]["application/json"]["schema"]
    check = SOURCE.evolve(schema={**DOCUMENT, "allOf": [schema]})
    check.validate(response.json())
    # A bare framework detail or a truncated envelope must not be accepted.
    with pytest.raises(ValidationError):
        check.validate({"detail": "Method Not Allowed"})
    invalid = copy.deepcopy(response.json())
    del invalid["error"]["request_id"]
    with pytest.raises(ValidationError):
        check.validate(invalid)


@pytest.mark.parametrize("case", CASES["method_not_allowed"], ids=lambda case: case["operation"])
async def test_wrong_method_uses_declared_error_schema(case: dict[str, str]) -> None:
    # Enable ingress without reading storage: unsupported methods never call it.
    assets = Mock(spec=AssetService)
    app = create_app(settings=_settings(), assets=assets)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        response = await http.put(case["path"], headers={"X-Request-ID": "status-example"})
    assert response.status_code == 405
    assert response.headers["allow"] == case["allow"]
    assert response.json()["error"]["code"] == case["code"]
    assert response.json()["error"]["request_id"] == "status-example"
    assert response.json()["error"]["retryable"] is False
    assert_error_contract(case["operation"], response)
    assert assets.mock_calls == []


@pytest.mark.parametrize("case", CASES["errors"], ids=lambda case: case["operation"])
async def test_transcription_not_found_and_invalid_cancellation(case: dict[str, Any]) -> None:
    jobs, _, _ = job_service()
    transcriptions, _, _ = chain()
    app = create_app(settings=_settings(), jobs=jobs, transcriptions=transcriptions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        if case["operation"] == "createTranscription":
            response = await http.post(case["path"], data={"model": "unpublished"}, files=files())
        else:
            response = await http.post(case["path"], headers={"Idempotency-Key": "short"})
    assert response.status_code == case["status"]
    assert response.json()["error"]["code"] == case["code"]
    assert_error_contract(case["operation"], response)


@pytest.mark.parametrize("case", CASES["artifacts"], ids=lambda case: case["operation"])
async def test_artifact_conditional_range_and_unavailable_replies(case: dict[str, str]) -> None:
    artifacts, _, _ = service()
    path = case["path"].replace("{artifact_id}", ARTIFACT_ID)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings=_settings(), artifacts=artifacts)),
        base_url="http://localhost",
    ) as http:
        initial = await http.get(path)
        cached = await http.get(path, headers={"If-None-Match": initial.headers["etag"]})
        if case["operation"] == "downloadArtifactContent":
            unsatisfiable = await http.get(path, headers={"Range": f"bytes={len(CONTENT)}-"})
            assert unsatisfiable.status_code == 416
            assert unsatisfiable.headers["content-range"] == f"bytes */{len(CONTENT)}"
            assert_error_contract(case["operation"], unsatisfiable)
    assert cached.status_code == 304 and cached.content == b""
    contract = response_contract(case["operation"], 304)
    assert not contract.get("content")
    assert "ETag" in contract["headers"]
    assert cached.headers["etag"] == initial.headers["etag"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings=_settings())),
        base_url="http://localhost",
    ) as http:
        unavailable = await http.get(path)
    assert unavailable.status_code == 503
    assert_error_contract(case["operation"], unavailable)
