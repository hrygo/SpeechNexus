"""Collection boundary: one keyset read, one immutable draft create, no publication."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.pronunciation_sets_http import validate_response

from speechnexus_backend.app import create_app
from speechnexus_backend.pronunciation_runtime import PronunciationWriterRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.domain.pronunciation_drafts import (
    CreatePronunciationDraft,
    PronunciationDraft,
    PronunciationDraftResult,
    PronunciationSetPage,
    PronunciationSummary,
    PronunciationUnavailableError,
)
from speechnexus_backend.speech.interfaces.http.pronunciation_sets import (
    CREATE_OPERATION,
    LIST_OPERATION,
    PATH,
    cursor_for,
)
from speechnexus_backend.speech.interfaces.pronunciation_drafts import prepare_pronunciation_draft
from tests.support.pronunciation_examples import pronunciation_request
from tests.unit.backend.test_health import _settings

REQUEST_ID = "pronunciation-sets-484"
KEY = "synthetic-idempotency-key"


def summary(pronunciation_id: str, revision: str = "1", count: int = 2) -> PronunciationSummary:
    return PronunciationSummary(pronunciation_id, revision, "pinyin", count)


def draft(command: CreatePronunciationDraft) -> PronunciationDraft:
    return PronunciationDraft("pron-created", "1", command.definition, 1)


class Catalogue:
    """In-memory list/write port; the collection may only see these two methods."""

    def __init__(self) -> None:
        self.pages: list[PronunciationSummary] = []
        self.error: Exception | None = None
        self.results: list[PronunciationDraftResult] = []
        self.commands: list[CreatePronunciationDraft] = []
        self.listed: list[tuple[int, str | None]] = []

    def get(self, pronunciation_id: str, revision: str) -> PronunciationDraft | None:
        return None

    def list_sets(self, *, limit: int, after: str | None) -> PronunciationSetPage:
        self.listed.append((limit, after))
        if self.error is not None:
            raise self.error
        start = (
            0
            if after is None
            else next(
                index + 1 for index, item in enumerate(self.pages) if item.pronunciation_id == after
            )
        )
        window = self.pages[start : start + limit + 1]
        return PronunciationSetPage(tuple(window[:limit]), len(window) > limit)

    def create(self, command: CreatePronunciationDraft) -> PronunciationDraftResult:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return (
            self.results.pop(0)
            if self.results
            else PronunciationDraftResult("created", draft(command))
        )

    def recover(self, command: CreatePronunciationDraft) -> PronunciationDraftResult:
        raise AssertionError("collection create never recovers directly")


def app(
    port: Catalogue | None = None,
    *,
    writer: Catalogue | None = None,
    **changes: Any,
) -> Any:
    return create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("test",), **changes}),
        pronunciations=port,
        pronunciation_writer=writer,
    )


def client(app_: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app_), base_url="http://test")


def check(response: Any, status: int, operation: str = CREATE_OPERATION) -> None:
    assert response.status_code == status, response.text
    validate_response(
        operation,
        status,
        {"body": response.json(), "headers": dict(response.headers)},
    )
    assert response.headers["cache-control"] == "no-store"
    if status != 200 and status != 201:
        error = response.json()["error"]
        assert error["request_id"] == response.headers["x-request-id"]
        assert "nexus" not in error
        if "x-should-retry" in response.headers:
            assert error["retryable"] is (response.headers["x-should-retry"] == "true")


async def test_empty_collection_is_a_successful_page_and_never_a_missing_one() -> None:
    port = Catalogue()
    async with client(app(port)) as http:
        response = await http.get(PATH, headers={"X-Request-ID": REQUEST_ID})
        check(response, 200, LIST_OPERATION)
        body = response.json()
        assert body == {
            "object": "nexus.list",
            "data": [],
            "has_more": False,
            "next_cursor": None,
        }
        assert response.headers["x-request-id"] == REQUEST_ID
        assert port.listed == [(50, None)]


async def test_keyset_cursor_is_canonical_and_tampering_cannot_move_the_page() -> None:
    port = Catalogue()
    port.pages = [summary("pron-aaa"), summary("pron-bbb", "2")]
    async with client(app(port)) as http:
        first = await http.get(PATH, params={"limit": 1})
        check(first, 200, LIST_OPERATION)
        body = first.json()
        assert [item["id"] for item in body["data"]] == ["pron-aaa"]
        assert body["has_more"] is True
        cursor = body["next_cursor"]
        assert cursor == cursor_for("pron-aaa")
        assert cursor == base64.urlsafe_b64encode(b"pron-aaa").decode().rstrip("=")

        second = await http.get(PATH, params={"limit": 1, "cursor": cursor})
        check(second, 200, LIST_OPERATION)
        assert [item["id"] for item in second.json()["data"]] == ["pron-bbb"]
        assert second.json()["has_more"] is False
        assert second.json()["next_cursor"] is None
        assert port.listed[-1] == (1, "pron-aaa")

        for tampered in (cursor + "=", cursor + "A", "not-base64!", "eA"):
            rejected = await http.get(PATH, params={"cursor": tampered})
            check(rejected, 400, LIST_OPERATION)
            assert rejected.json()["error"]["code"] == "pronunciation_invalid_request"


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=abc",
        "limit=1&limit=2",
        "cursor=" + "A" * 513,
        "unknown=1",
    ],
)
async def test_invalid_collection_query_is_an_explicit_400(query: str) -> None:
    port = Catalogue()
    async with client(app(port)) as http:
        response = await http.get(PATH + "?" + query)
        check(response, 400, LIST_OPERATION)
        assert response.json()["error"]["code"] == "pronunciation_invalid_request"
        assert port.listed == []


async def test_a_read_body_is_rejected_instead_of_being_ignored() -> None:
    port = Catalogue()
    async with client(app(port)) as http:
        for headers, content in (
            ({"Content-Type": "application/json"}, b"{}"),
            ({"Transfer-Encoding": "chunked", "Content-Type": "application/json"}, b"{}"),
        ):
            response = await http.request("GET", PATH, headers=headers, content=content)
            check(response, 400, LIST_OPERATION)
        assert port.listed == []


async def test_create_omission_records_a_draft_and_publication_is_unsupported() -> None:
    port, writer = Catalogue(), Catalogue()
    body = pronunciation_request()
    body.pop("publish")
    async with client(app(port, writer=writer)) as http:
        response = await http.post(PATH, json=body, headers={"Idempotency-Key": KEY})
        check(response, 201)
        assert response.json()["status"] == "draft"
        assert json.loads(writer.commands[0].intent)["publish"] is False

        published = await http.post(
            PATH, json={**body, "publish": True}, headers={"Idempotency-Key": KEY}
        )
        check(published, 422)
        assert published.json()["error"]["code"] == "pronunciation_publication_not_supported"
        assert published.headers["x-should-retry"] == "false"
        assert len(writer.commands) == 1

        for bad in (
            {**body, "publish": "yes"},
            {**body, "encoding": "kana"},
            {**body, "entries": []},
            {**body, "id": "pron-aaa"},
        ):
            rejected = await http.post(PATH, json=bad, headers={"Idempotency-Key": KEY})
            check(rejected, 400)
            assert rejected.json()["error"]["code"] == "pronunciation_invalid_request"
        assert len(writer.commands) == 1


async def test_create_maps_store_outcomes_to_retry_safe_failures() -> None:
    port, writer = Catalogue(), Catalogue()
    body = pronunciation_request()
    async with client(app(port, writer=writer)) as http:
        assert (
            await http.post(PATH, json=body, headers={"Idempotency-Key": KEY})
        ).status_code == 201

        writer.results = [PronunciationDraftResult("conflict", error_code="idempotency_conflict")]
        conflict = await http.post(PATH, json=body, headers={"Idempotency-Key": KEY})
        check(conflict, 409)
        assert conflict.json()["error"]["code"] == "pronunciation_idempotency_conflict"
        assert conflict.headers["x-should-retry"] == "false"

        writer.results = [PronunciationDraftResult("rejected", error_code="configuration_changed")]
        stale = await http.post(PATH, json=body, headers={"Idempotency-Key": KEY})
        check(stale, 409)
        assert stale.json()["error"]["code"] == "pronunciation_configuration_changed"

        writer.results = [PronunciationDraftResult("unknown")]
        keyless = await http.post(PATH, json=body)
        check(keyless, 409)
        assert keyless.json()["error"]["code"] == "pronunciation_outcome_unknown"
        assert keyless.headers["x-should-retry"] == "false"

        writer.results = [PronunciationDraftResult("unknown")]
        keyed = await http.post(PATH, json=body, headers={"Idempotency-Key": KEY})
        check(keyed, 503)
        assert keyed.json()["error"]["code"] == "pronunciation_unavailable"
        assert keyed.headers["x-should-retry"] == "true"

        writer.results = [PronunciationDraftResult("created", None)]
        absent = await http.post(PATH, json=body, headers={"Idempotency-Key": KEY})
        check(absent, 500)
        assert absent.json()["error"]["code"] == "pronunciation_contract_violation"
        assert absent.headers["x-should-retry"] == "false"

        writer.results = []
        writer.error = PronunciationUnavailableError()
        unavailable = await http.post(PATH, json=body, headers={"Idempotency-Key": KEY})
        check(unavailable, 503)
        assert unavailable.json()["error"]["code"] == "pronunciation_unavailable"


async def test_a_deterministic_reader_defect_is_a_500_not_a_retryable_503() -> None:
    port = Catalogue()
    port.error = RuntimeError("summary projection is broken")
    async with client(app(port)) as http:
        response = await http.get(PATH)
        check(response, 500, LIST_OPERATION)
        assert response.json()["error"]["code"] == "pronunciation_contract_violation"
        assert response.json()["error"]["type"] == "api_error"
        assert response.json()["error"]["retryable"] is False
        assert response.headers["x-should-retry"] == "false"


async def test_a_deterministic_writer_defect_is_a_500_not_a_retryable_503() -> None:
    writer = Catalogue()
    writer.error = RuntimeError("command encoding is broken")
    async with client(app(writer=writer)) as http:
        response = await http.post(
            PATH, json=pronunciation_request(), headers={"Idempotency-Key": KEY}
        )
        check(response, 500, CREATE_OPERATION)
        assert response.json()["error"]["code"] == "pronunciation_contract_violation"
        assert response.json()["error"]["retryable"] is False
        assert response.headers["x-should-retry"] == "false"


async def test_list_unavailability_is_retryable_and_never_a_404() -> None:
    port = Catalogue()
    port.error = PronunciationUnavailableError()
    async with client(app(port)) as http:
        response = await http.get(PATH)
        check(response, 503, LIST_OPERATION)
        assert response.json()["error"]["code"] == "pronunciation_unavailable"
        assert response.headers["x-should-retry"] == "true"


async def test_a_missing_writer_answers_503_instead_of_silently_succeeding() -> None:
    port = Catalogue()
    async with client(app(port)) as http:
        response = await http.post(
            PATH, json=pronunciation_request(), headers={"Idempotency-Key": KEY}
        )
        check(response, 503)
        assert response.json()["error"]["code"] == "pronunciation_unavailable"


async def test_body_limit_and_key_framing_are_enforced_by_the_route() -> None:
    port, writer = Catalogue(), Catalogue()
    body = pronunciation_request()
    async with client(app(port, writer=writer, pronunciation_receive_max_bytes=1024)) as http:
        oversized = await http.post(
            PATH,
            content=b'{"encoding":"pinyin","entries":[],"padding":"' + b"x" * 2048 + b'"}',
            headers={"Content-Type": "application/json", "Idempotency-Key": KEY},
        )
        check(oversized, 400)
        assert oversized.json()["error"]["code"] == "pronunciation_invalid_request"
        for headers in (
            {"Content-Type": "application/json", "Idempotency-Key": "short"},
            {"Content-Type": "text/plain", "Idempotency-Key": KEY},
            {"Idempotency-Key": KEY},
        ):
            rejected = await http.post(PATH, content=json.dumps(body).encode(), headers=headers)
            check(rejected, 400)
        assert writer.commands == []


async def test_host_origin_and_other_methods_stay_behind_the_entry_boundary() -> None:
    port = Catalogue()
    async with client(app(port)) as http:
        host = await http.get(PATH, headers={"Host": "evil.example"})
        check(host, 403, LIST_OPERATION)
        assert host.json()["error"]["code"] == "pronunciation_host_rejected"
        origin = await http.get(PATH, headers={"Origin": "https://evil.example"})
        check(origin, 403, LIST_OPERATION)
        assert origin.json()["error"]["code"] == "pronunciation_origin_rejected"
        wrong = await http.request("PUT", PATH, json=pronunciation_request())
        check(wrong, 405)
        assert port.listed == []


async def test_single_trailing_slash_is_starlettes_normal_redirect() -> None:
    port = Catalogue()
    async with client(app(port)) as http:
        response = await http.get(PATH + "/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"].endswith(PATH)
        followed = await http.get(PATH + "/", follow_redirects=True)
        check(followed, 200, LIST_OPERATION)


def test_served_openapi_declares_the_implemented_collection() -> None:
    document = app(Catalogue()).openapi()
    operation = document["paths"][PATH]
    assert set(operation) == {"get", "post"}
    assert operation["get"]["operationId"] == LIST_OPERATION
    assert operation["post"]["operationId"] == CREATE_OPERATION
    assert "400" in operation["get"]["responses"]
    assert "403" in operation["get"]["responses"]
    for method in ("get", "post"):
        unavailable = operation[method]["responses"]["503"]
        assert "$ref" not in unavailable
        assert unavailable["headers"]["X-Should-Retry"]["schema"]["const"] == "true"


def test_writer_settings_are_opt_in_bounded_and_api_only() -> None:
    from pydantic import ValidationError

    settings = _settings()
    assert settings.pronunciation_writer_database_url is None
    blank = Settings(**{**settings.model_dump(), "pronunciation_writer_database_url": " "})
    assert blank.pronunciation_writer_database_url is None
    with pytest.raises(ValueError, match="explicit API"):
        PronunciationWriterRuntime(settings)
    for updates in (
        {"pronunciation_write_concurrency": 0},
        {"pronunciation_receive_max_bytes": 0},
        {"pronunciation_transaction_timeout_seconds": 0},
        {"pronunciation_receive_timeout_seconds": 0},
    ):
        with pytest.raises(ValidationError):
            Settings(**{**settings.model_dump(), **updates})


def test_prepare_still_requires_an_explicit_draft_intent() -> None:
    body = pronunciation_request()
    body.pop("publish")
    with pytest.raises(ContractValidationError):
        prepare_pronunciation_draft(body, KEY)
    assert replace(summary("pron-aaa")).entry_count == 2
    assert base64.urlsafe_b64encode(b"pron-aaa").decode().rstrip("=") == "cHJvbi1hYWE"
