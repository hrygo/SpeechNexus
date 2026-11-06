"""Declared-but-unimplemented operations answer one stable, distinguishable error.

#473: every operation the frozen speech contract declares and this deployment
does not implement must be registered centrally and answer 503
``operation_not_implemented`` through the public error envelope. A consumer must
be able to tell that reply apart from a real ``backend_unavailable`` 503, a
confirmed missing resource and a wrong method.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.interfaces.http.job_errors import job_error
from speechnexus_backend.speech.interfaces.http.unimplemented import UNIMPLEMENTED_OPERATIONS
from speechnexus_backend.speech.interfaces.http.unimplemented_errors import (
    OPERATION_NOT_IMPLEMENTED,
)
from tests.contract.test_speech_http_statuses import assert_error_contract
from tests.support.contracts import validator
from tests.unit.backend.test_artifact_http import service
from tests.unit.backend.test_health import _settings

HTTP_ROOT = (
    Path(__file__).resolve().parents[2] / "backend/src/speechnexus_backend/speech/interfaces/http"
)
SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
PLACEHOLDER_RESPONSE = "#/components/responses/OperationNotImplemented"
REQUEST_ID = "unimplemented-473"
# These remaining placeholders already declared `ServiceUnavailable` before #473.
# Replacing that declaration would drop the compatible `Retry-After` header
# from the versioned surface, which both structural gates reject as breaking,
# so they keep it: that schema accepts the same placeholder body.
PREDECLARED_SERVICE_UNAVAILABLE = frozenset(
    {
        "listPronunciationSets",
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


def error_maps() -> dict[Path, set[str]]:
    """Error codes declared by each `*_errors.py` map, read as literals."""
    maps: dict[Path, set[str]] = {}
    for path in sorted(HTTP_ROOT.glob("*_errors.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        codes: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                targets: list[ast.expr] = [node.target]
            elif isinstance(node, ast.Assign):
                targets = list(node.targets)
            else:
                continue
            if not any(isinstance(t, ast.Name) and t.id == "_ERRORS" for t in targets):
                continue
            if isinstance(node.value, ast.Dict):
                codes.update(
                    key.value
                    for key in node.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
        maps[path] = codes
    return maps


def concrete(path: str) -> str:
    while "{" in path:
        start = path.index("{")
        end = path.index("}", start)
        path = path[:start] + "synthetic-id" + path[end + 1 :]
    return path


def test_contract_and_placeholder_table_agree() -> None:
    assert len(CASES) == 20
    assert {operation for operation, _, _ in CASES} == set(UNIMPLEMENTED_OPERATIONS)
    declared = {operation for operation, _, _ in declared_operations()}
    assert len(declared) == 16
    # Every remaining operation points at the dedicated placeholder component;
    # the four with an older 503 declaration keep it rather than lose a header.
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
        unavailable = await http.get("/v1/nexus/artifacts/synthetic-id")
    assert unimplemented.status_code == unavailable.status_code == 503
    assert unimplemented.json()["error"]["type"] == unavailable.json()["error"]["type"]
    # The code is the only reliable discriminator: status and type are shared,
    # and a real outage can also be non-retryable (see the next test).
    assert unimplemented.json()["error"]["code"] == OPERATION_NOT_IMPLEMENTED
    assert unavailable.json()["error"]["code"] != OPERATION_NOT_IMPLEMENTED


def test_status_type_and_retryable_cannot_identify_the_placeholder() -> None:
    # `job_unavailable` is 503 + service_unavailable_error + retryable=false, so
    # only `error.code` separates a real failure from "not implemented".
    body = json.loads(bytes(job_error("job_unavailable", REQUEST_ID).body))
    assert body["error"]["type"] == "service_unavailable_error"
    assert body["error"]["retryable"] is False
    assert body["error"]["code"] != OPERATION_NOT_IMPLEMENTED


def test_no_other_error_map_reuses_the_placeholder_code() -> None:
    owners = {
        module.stem
        for module, entries in error_maps().items()
        if OPERATION_NOT_IMPLEMENTED in entries
    }
    assert owners == {"unimplemented_errors"}


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


@pytest.mark.parametrize(
    ("path", "allow", "code"),
    [
        # Both listJobs and createJob are implemented: the header must still
        # merge both, not repeat whichever route matched first.
        ("/v1/nexus/jobs", "GET, POST", "job_method_not_allowed"),
        # Both methods live on a parameterised template, so a literal path
        # comparison used to report only the first of them.
        ("/v1/nexus/voice-drafts/synthetic-id", "GET, PATCH", "method_not_allowed"),
        ("/v1/nexus/pronunciation-sets", "GET, POST", "method_not_allowed"),
        ("/v1/nexus/voices/voice-demo/revisions/1/retirements", "POST", "method_not_allowed"),
    ],
)
async def test_allow_header_lists_every_registered_method(path: str, allow: str, code: str) -> None:
    app = create_app(settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        response = await http.delete(path)
    assert response.status_code == 405
    assert response.headers["allow"] == allow
    # The Job prefix keeps its own 405 code; only the Allow list grows.
    assert response.json()["error"]["code"] == code


def test_a_real_route_cannot_share_a_placeholder_method() -> None:
    from fastapi import FastAPI

    from speechnexus_backend.speech.interfaces.http.unimplemented import (
        assert_placeholder_ownership,
    )

    api = FastAPI()

    @api.get("/v1/nexus/voice-drafts/{draft_id}", include_in_schema=False)
    async def shadowed(draft_id: str) -> dict[str, str]:
        return {"draft_id": draft_id}

    with pytest.raises(RuntimeError, match="GET /v1/nexus/voice-drafts"):
        assert_placeholder_ownership(api.routes)


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
