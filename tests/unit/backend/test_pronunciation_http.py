"""Actual read boundary: fixed identity, source errors and bounded cancellation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from threading import Event
from typing import Any
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts.pronunciation_read_http import validate_response

from speechnexus_backend.app import create_app
from speechnexus_backend.pronunciation_runtime import PronunciationRuntime, _PoolDiagnostics
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.domain.pronunciation_drafts import PronunciationDraft
from speechnexus_backend.speech.interfaces.http.pronunciations import OPERATION, PATH
from speechnexus_backend.speech.interfaces.pronunciation_drafts import prepare_pronunciation_draft
from tests.support.pronunciation_examples import pronunciation_request
from tests.unit.backend.test_health import _settings

GET = PATH.format(pronunciation_id="pron-synthetic", revision="1")


def draft() -> PronunciationDraft:
    return PronunciationDraft(
        "pron-synthetic", "1", prepare_pronunciation_draft(pronunciation_request()).definition, 0
    )


def app(port: Any = None) -> Any:
    return create_app(
        settings=_settings().model_copy(
            update={
                "asset_allowed_hosts": ("test",),
                "pronunciation_read_concurrency": 1,
            }
        ),
        pronunciations=port,
    )


def check(response: Any, status: int) -> None:
    assert response.status_code == status, response.text
    validate_response(
        OPERATION,
        status,
        {
            "body": response.json(),
            "headers": dict(response.headers),
        },
    )
    assert response.headers["cache-control"] == "no-store"
    if status != 200:
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
        assert "nexus" not in response.json()["error"]


async def test_fixed_read_distinguishes_unknown_absence_and_wrong_identity() -> None:
    port = Mock()
    port.get.return_value = draft()
    async with AsyncClient(transport=ASGITransport(app(port)), base_url="http://test") as client:
        response = await client.get(GET)
        check(response, 200)
        assert response.json()["status"] == "draft"
        assert response.json()["entries"] == pronunciation_request()["entries"]
        assert not {"key_digest", "request_digest", "definition_json"}.intersection(response.json())
        for wrong in (
            replace(draft(), pronunciation_id="pron-other"),
            replace(draft(), revision="2"),
        ):
            port.get.return_value = wrong
            check(await client.get(GET), 503)
        port.get.return_value = None
        missing = await client.get(GET)
        check(missing, 404)
        assert missing.json()["error"]["code"] == "pronunciation_not_found"
        port.get.side_effect = RuntimeError("synthetic-private-driver-canary")
        unavailable = await client.get(GET)
        check(unavailable, 503)
        assert unavailable.json()["error"]["retryable"] is True
        assert "synthetic-private-driver-canary" not in unavailable.text
    port.create.assert_not_called()


@pytest.mark.parametrize(
    "method,path,headers,content,status",
    [
        ("GET", GET, {"Host": "foreign.test"}, None, 403),
        ("GET", GET, {"Origin": "null"}, None, 403),
        ("POST", GET, {}, None, 405),
        ("HEAD", GET, {}, None, 405),
        ("GET", GET.replace("/1", "/0"), {}, None, 400),
        ("GET", GET.replace("/1", "/current"), {}, None, 400),
        ("GET", GET.replace("/1", "/10000000000000000000"), {}, None, 400),
        ("GET", GET + "?extra=1", {}, None, 400),
        ("GET", GET + "?extra=1&extra=2", {}, None, 400),
        ("GET", GET, {"Content-Length": "1"}, b"x", 400),
        ("GET", GET, {"Transfer-Encoding": "chunked"}, b"", 400),
        ("GET", GET, {"X-Request-ID": "x" * 129}, None, 400),
    ],
)
async def test_invalid_ingress_never_reads(
    method: str,
    path: str,
    headers: dict[str, str],
    content: bytes | None,
    status: int,
) -> None:
    port = Mock()
    async with AsyncClient(transport=ASGITransport(app(port)), base_url="http://test") as client:
        response = await client.request(method, path, headers=headers, content=content)
        if method == "HEAD":
            assert response.status_code == status and response.headers["allow"] == "GET"
        else:
            check(response, status)
    port.get.assert_not_called()


async def test_unconfigured_is_503_and_duplicate_headers_are_rejected() -> None:
    async with AsyncClient(transport=ASGITransport(app()), base_url="http://test") as client:
        check(await client.get(GET), 503)
    port = Mock()
    async with AsyncClient(transport=ASGITransport(app(port)), base_url="http://test") as client:
        for headers in (
            [("X-Request-ID", "a"), ("X-Request-ID", "b")],
            [("Content-Length", "0"), ("Content-Length", "0")],
        ):
            check(await client.get(GET, headers=headers), 400)
    port.get.assert_not_called()


async def test_cancelled_read_holds_capacity_until_real_thread_finishes() -> None:
    started, release = Event(), Event()
    port = Mock()

    def read(*args: Any) -> PronunciationDraft:
        started.set()
        assert release.wait(3)
        return draft()

    port.get.side_effect = read
    async with AsyncClient(transport=ASGITransport(app(port)), base_url="http://test") as client:
        task = asyncio.create_task(client.get(GET))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            check(await client.get(GET), 429)
            task.cancel()
            await asyncio.sleep(0)
            check(await client.get(GET), 429)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        check(await client.get(GET), 200)
    assert port.get.call_count == 2


def test_served_openapi_has_only_fixed_get_and_preserves_existing_surfaces() -> None:
    document = app().openapi()
    assert set(document["paths"][PATH]) == {"get"}
    assert document["paths"][PATH]["get"]["operationId"] == OPERATION
    assert document["paths"][PATH]["get"]["responses"].keys() == {
        "200",
        "400",
        "401",
        "403",
        "404",
        "405",
        "429",
        "503",
    }
    assert "/v1/nexus/pronunciation-sets" not in document["paths"]
    assert "/v1/nexus/admin/profiles" in document["paths"]


def test_reader_settings_are_opt_in_bounded_and_api_only() -> None:
    from pydantic import ValidationError

    settings = _settings()
    assert settings.pronunciation_reader_database_url is None
    blank = Settings(**{**settings.model_dump(), "pronunciation_reader_database_url": " "})
    assert blank.pronunciation_reader_database_url is None
    with pytest.raises(ValueError, match="explicit API"):
        PronunciationRuntime(settings)
    for updates in (
        {"pronunciation_read_concurrency": 0},
        {"pronunciation_read_concurrency": 5},
        {"pronunciation_read_timeout_seconds": 0},
        {"pronunciation_read_timeout_seconds": 31},
    ):
        with pytest.raises(ValidationError):
            Settings(**{**settings.model_dump(), **updates})


def test_pool_diagnostics_do_not_expose_connection_failure_details() -> None:
    diagnostic = _PoolDiagnostics("synthetic-pool")
    record = logging.LogRecord(
        "psycopg.pool",
        logging.WARNING,
        "",
        1,
        "error in %s: synthetic-private-dsn",
        ("synthetic-pool",),
        None,
    )
    record.exc_text = "synthetic-private-driver-error"
    assert diagnostic.filter(record)
    assert record.getMessage() == "Pronunciation database connection unavailable"
    assert record.__dict__["exc_text"] is None
    assert record.exc_info is None
