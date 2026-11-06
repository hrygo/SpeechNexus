"""Declared-but-unimplemented operations answer one stable, distinguishable error.

#473: every operation the frozen speech contract declares and this deployment
does not implement must be registered centrally and answer 503
``operation_not_implemented`` through the public error envelope. A consumer must
be able to tell that reply apart from a real ``backend_unavailable`` 503, a
confirmed missing resource and a wrong method.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.interfaces.http.unimplemented import UNIMPLEMENTED_OPERATIONS
from speechnexus_backend.speech.interfaces.http.unimplemented_errors import (
    OPERATION_NOT_IMPLEMENTED,
)
from tests.contract.test_speech_http_statuses import assert_error_contract
from tests.support.contracts import validator
from tests.unit.backend.test_artifact_http import service
from tests.unit.backend.test_health import _settings

SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
PLACEHOLDER_RESPONSE = "#/components/responses/OperationNotImplemented"
REQUEST_ID = "unimplemented-473"
# These five operations already declared `ServiceUnavailable` before #473.
# Replacing that declaration would drop the compatible `Retry-After` header
# from the versioned surface, which both structural gates reject as breaking,
# so they keep it: that schema accepts the same placeholder body.
PREDECLARED_SERVICE_UNAVAILABLE = frozenset(
    {
        "listPronunciationSets",
        "listJobs",
        "getOperation",
        "createRealtimeSession",
        "connectRealtime",
    }
)
# Every placeholder answers at runtime, including the compatible-surface one.
CASES = sorted(
    (operation, method, path) for operation, (method, path) in UNIMPLEMENTED_OPERATIONS.items()
)


def declared_operations() -> list[tuple[str, str, str]]:
    """Every operation whose 503 is the placeholder component, straight from the contract."""
    found: list[tuple[str, str, str]] = []
    for path, item in DOCUMENT["paths"].items():
        for method, specification in item.items():
            responses = specification.get("responses", {})
            reference = responses.get("503", {}).get("$ref")
            if reference == PLACEHOLDER_RESPONSE:
                found.append((specification["operationId"], method.upper(), path))
    return sorted(found)


def concrete(path: str) -> str:
    while "{" in path:
        start = path.index("{")
        end = path.index("}", start)
        path = path[:start] + "synthetic-id" + path[end + 1 :]
    return path


def test_contract_and_placeholder_table_agree() -> None:
    assert len(CASES) == 21
    assert {operation for operation, _, _ in CASES} == set(UNIMPLEMENTED_OPERATIONS)
    declared = {operation for operation, _, _ in declared_operations()}
    assert len(declared) == 16
    # Every remaining operation points at the dedicated placeholder component;
    # the five with an older 503 declaration keep it rather than lose a header.
    assert declared == set(UNIMPLEMENTED_OPERATIONS) - PREDECLARED_SERVICE_UNAVAILABLE


@pytest.mark.parametrize("case", CASES, ids=lambda case: case[0])
async def test_unimplemented_operation_answers_the_stable_error(
    case: tuple[str, str, str],
) -> None:
    operation, method, path = case
    app = create_app(settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        response = await http.request(
            method,
            concrete(path),
            headers={"X-Request-ID": REQUEST_ID, "Content-Type": "application/json"},
            content=b"{}" if method in {"POST", "PATCH", "PUT"} else None,
        )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == OPERATION_NOT_IMPLEMENTED
    assert body["error"]["type"] == "service_unavailable_error"
    assert body["error"]["retryable"] is False
    assert body["error"]["param"] is None
    assert body["error"]["request_id"] == REQUEST_ID
    # Correlation reaches the transport header as well as the envelope.
    assert response.headers["x-request-id"] == REQUEST_ID
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-should-retry"] == "false"
    # The declared placeholder response pins code, type and retryable.
    assert_error_contract(operation, response)


async def test_unimplemented_error_is_not_a_real_failure() -> None:
    app = create_app(settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        unimplemented = await http.get(
            "/v1/nexus/voice-drafts/synthetic-id", headers={"X-Request-ID": REQUEST_ID}
        )
        # A real outage keeps its own code: same status and type, different code
        # and a retryable flag that actually means "try again".
        unavailable = await http.get("/v1/nexus/artifacts/synthetic-id")
    assert unimplemented.status_code == unavailable.status_code == 503
    assert unimplemented.json()["error"]["type"] == unavailable.json()["error"]["type"]
    assert unimplemented.json()["error"]["code"] == OPERATION_NOT_IMPLEMENTED
    assert unavailable.json()["error"]["code"] != OPERATION_NOT_IMPLEMENTED
    assert unimplemented.json()["error"]["retryable"] is False
    assert unavailable.json()["error"]["retryable"] is True


async def test_unimplemented_error_is_not_a_missing_resource_or_bad_method() -> None:
    artifacts, _, _ = service()
    app = create_app(settings=_settings(), artifacts=artifacts)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        missing = await http.get("/v1/nexus/artifacts/synthetic-id")
        wrong_method = await http.put(
            "/v1/nexus/voice-designs", headers={"X-Request-ID": REQUEST_ID}
        )
        unregistered = await http.get("/v1/nexus/operations/synthetic-id/unknown-child")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "artifact_not_found"
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"
    # A path with no route is still the framework's 404, not a placeholder.
    assert unregistered.status_code == 404
    assert "error" not in unregistered.json()


async def test_placeholder_methods_are_listed_together_with_implemented_ones() -> None:
    app = create_app(settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        # GET listJobs is a placeholder while POST createJob is implemented.
        response = await http.delete("/v1/nexus/jobs")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, POST"
    # The Job prefix keeps its own 405 code; only the Allow list grows.
    assert response.json()["error"]["code"] == "job_method_not_allowed"


def test_served_openapi_does_not_advertise_placeholders() -> None:
    paths = create_app(settings=_settings()).openapi()["paths"]
    for path in (
        "/v1/nexus/voice-designs",
        "/v1/nexus/voice-clones",
        "/v1/nexus/voice-drafts/{draft_id}",
        "/v1/nexus/pronunciation-sets",
        "/v1/realtime",
    ):
        assert path not in paths
