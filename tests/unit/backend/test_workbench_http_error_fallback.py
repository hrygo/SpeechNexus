"""Refusals that outlive the generated declarations they were written against.

`generateRenderUnitV2`, the three assistant-turn operations and the three book-export
operations never declared a `500`; `getAssistantTurn`/`getBookExport` declare neither
`400` nor `503`. The error layer builds those envelopes from its static table and checks
the operation's declarations afterwards, so a refusal it was never told about must still
reach the caller as the structured `ErrorResponse` instead of a bare `text/plain` 500
from the ASGI host. These tests pin the envelope, its correlation, its cache and retry
headers, the absence of the original failure's text, and the log line that keeps the
declaration gap observable.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import validate_response

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.version_ports import VersionStoreError

PRIVATE = "private-synthetic-777"
LOGGER = "speechnexus_backend.workbench.interfaces.http_versions"
IDEMPOTENT = {"Idempotency-Key": "key-777-0001"}
DUPLICATED = [("X-Request-ID", "rid-777-probe"), ("X-Request-ID", "rid-777-probe")]

TURN_BODY = {"input_text": "合成提问", "voice": {"id": "voice_demo", "revision": "1"}}
EXPORT_BODY = {"expected_casting_revision": "1", "format": "zip"}
GENERATION_BODY = {"expected_unit_revision": "1"}

# operation, method, path, payload, and whether that operation declares the status the
# injected failure answers with. The undeclared cases are the defect #777 fixes; the
# declared one proves the ordinary validated path is unchanged.
UNEXPECTED = [
    (
        "createAssistantTurn",
        "POST",
        "/v1/workbench/projects/project_1/assistant-turns",
        TURN_BODY,
        False,
    ),
    ("getAssistantTurn", "GET", "/v1/workbench/assistant-turns/turn_1", None, False),
    (
        "cancelAssistantTurn",
        "POST",
        "/v1/workbench/assistant-turns/turn_1/cancellations",
        None,
        False,
    ),
    ("createBookExport", "POST", "/v1/workbench/books/book_1/exports", EXPORT_BODY, False),
    ("getBookExport", "GET", "/v1/workbench/exports/export_1", None, False),
    ("cancelBookExport", "POST", "/v1/workbench/exports/export_1/cancellations", None, False),
    (
        "generateRenderUnitV2",
        "POST",
        "/v2/workbench/render-units/unit_1/generations",
        GENERATION_BODY,
        False,
    ),
    ("getRenderUnitV2", "GET", "/v2/workbench/render-units/unit_1", None, True),
]

# The 400 and 503 counterparts of the same gap, plus one operation that declares each so
# a deviation in the ordinary path cannot hide behind the fallback.
MALFORMED = UNEXPECTED[:2] + UNEXPECTED[4:5] + UNEXPECTED[6:]
STORE_FAILURE = [UNEXPECTED[1], UNEXPECTED[4], UNEXPECTED[6]]


class _FailingService:
    """Every operation raises the injected failure; none of them is expected to answer."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    async def get_turn(self, **_: Any) -> Any:
        raise self.failure

    async def create_turn(self, **_: Any) -> Any:
        raise self.failure

    async def cancel_turn(self, **_: Any) -> Any:
        raise self.failure

    async def get_export(self, **_: Any) -> Any:
        raise self.failure

    async def create_export(self, **_: Any) -> Any:
        raise self.failure

    async def cancel_export(self, **_: Any) -> Any:
        raise self.failure

    async def get_unit(self, **_: Any) -> Any:
        raise self.failure

    async def start_unit(self, **_: Any) -> Any:
        raise self.failure


def app(failure: BaseException) -> FastAPI:
    service = _FailingService(failure)
    return create_app(
        None,
        render_units=service,  # type: ignore[arg-type]
        assistant=service,  # type: ignore[arg-type]
        exports=service,  # type: ignore[arg-type]
        default_workspace_id="workspace-synthetic",
        deployment_id="synthetic",
        cursor_key=b"synthetic-32-byte-cursor-key-00000",
        max_json_bytes=4096,
        body_timeout_seconds=0.05,
    )


async def call(
    failure: BaseException,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    headers: Any = None,
) -> httpx.Response:
    # `raise_app_exceptions=False` is the point: an escaping refusal reaches the client
    # as the bare transport-level 500 this suite must keep out of the surface.
    transport = httpx.ASGITransport(app=app(failure), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://workbench.test") as c:
        return await c.request(method, path, json=payload, headers=headers or IDEMPOTENT)


def envelope(response: httpx.Response, operation: str, status: int, code: str) -> Any:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json", response.text
    # Structural validity is asserted through the same generated schema the producer
    # uses: a fallback that skipped it would return JSON the contract never allows.
    body = parse("ErrorResponse", response.json())
    error = body["error"]
    assert error["code"] == code
    assert error["request_id"] == response.headers["x-request-id"]
    assert error["retryable"] is False
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-should-retry"] == "false"
    assert PRIVATE not in response.text and "Internal Server Error" not in response.text
    return error


@pytest.mark.parametrize("operation,method,path,payload,declared", UNEXPECTED)
async def test_an_unexpected_failure_is_a_structured_500(
    operation: str, method: str, path: str, payload: dict[str, Any] | None, declared: bool
) -> None:
    response = await call(RuntimeError(PRIVATE), method, path, payload)
    error = envelope(response, operation, 500, "internal_error")
    if declared:
        # The declared operation still passes the ordinary per-operation gate.
        validate_response(
            operation, 500, {"body": response.json(), "headers": dict(response.headers)}
        )
    else:
        assert error["param"] is None


@pytest.mark.parametrize("operation,method,path,payload,declared", MALFORMED)
async def test_a_malformed_request_is_a_structured_400(
    operation: str, method: str, path: str, payload: dict[str, Any] | None, declared: bool
) -> None:
    response = await call(RuntimeError(PRIVATE), method, path, payload, headers=DUPLICATED)
    envelope(response, operation, 400, "malformed_request")


@pytest.mark.parametrize("operation,method,path,payload,declared", STORE_FAILURE)
@pytest.mark.parametrize(
    "failure,code",
    [
        (VersionStoreError(sqlstate=PRIVATE, commit_unknown=False), "store_unavailable"),
        (VersionStoreError(sqlstate=PRIVATE, commit_unknown=True), "outcome_unknown"),
    ],
)
async def test_a_store_failure_is_a_structured_503(
    operation: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    declared: bool,
    failure: Exception,
    code: str,
) -> None:
    response = await call(failure, method, path, payload)
    envelope(response, operation, 503, code)


async def test_the_fallback_records_the_original_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    response = await call(
        RuntimeError(PRIVATE),
        "GET",
        "/v1/workbench/assistant-turns/turn_1",
        None,
        headers={"X-Request-ID": "rid-777-probe"},
    )
    envelope(response, "getAssistantTurn", 500, "internal_error")
    recorded = [record.getMessage() for record in caplog.records]
    assert any(
        "runtimeerror" in message.lower()
        and PRIVATE in message
        and "request_id=rid-777-probe" in message
        for message in recorded
    ), recorded
    assert any(
        "declaration-independent envelope" in message and "getAssistantTurn" in message
        for message in recorded
    ), recorded


async def test_the_declaration_gap_is_recorded_with_its_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    response = await call(
        VersionStoreError(sqlstate=PRIVATE, commit_unknown=False),
        "GET",
        "/v1/workbench/exports/export_1",
        None,
        headers={"X-Request-ID": "rid-777-probe"},
    )
    envelope(response, "getBookExport", 503, "store_unavailable")
    assert any(
        "declaration-independent envelope" in record.getMessage()
        and "operation=getBookExport" in record.getMessage()
        and "code=store_unavailable" in record.getMessage()
        and "request_id=rid-777-probe" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
