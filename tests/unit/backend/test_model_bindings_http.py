"""Cursor scope and request framing for the administrative binding list.

The cursor freezes the selected status filter by construction, so a cursor from
one filter cannot silently page another. The read declares no body, no repeated
parameter and only the contract's query fields.
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.binding_summaries import BindingSummaryService
from speechnexus_backend.speech.domain.binding_summaries import (
    BindingSummaryPage,
    BindingSummaryUnavailableError,
    ModelBindingSummary,
)
from speechnexus_backend.speech.interfaces.http.model_bindings import cursor_after, cursor_for
from tests.unit.backend.test_health import _settings

URL = "/v1/nexus/admin/model-bindings"


def row(**overrides: object) -> ModelBindingSummary:
    values: dict[str, object] = {
        "binding_id": "asr-quality",
        "latest_revision": "1",
        "current_revision": None,
        "logical_model": "whisper-large-v3",
        "tasks": ("asr_file",),
        "release_tier": "experimental",
        "publication_status": "draft",
    }
    values.update(overrides)
    return ModelBindingSummary(**values)  # type: ignore[arg-type]


class Stub:
    def __init__(
        self,
        page: BindingSummaryPage | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.page = page or BindingSummaryPage((row(),), "asr-quality")
        self.failure = failure
        self.queries: list[Any] = []

    def list(self, query: Any) -> BindingSummaryPage:
        self.queries.append(query)
        if self.failure is not None:
            raise self.failure
        return self.page


def settings() -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "asset_allowed_hosts": ("testserver",),
        }
    )


def client(stub: Stub) -> TestClient:
    service = cast(BindingSummaryService, stub)
    return TestClient(create_app(settings=settings(), binding_summaries=service))


def test_cursor_round_trips_and_is_bound_to_its_status_filter() -> None:
    for status in (None, "draft", "published", "retired", "disabled"):
        encoded = cursor_for("asr-quality", status)
        assert cursor_after(encoded, status) == "asr-quality"
        if status is not None:
            for other in ("draft", "published", "retired", "disabled"):
                if other != status:
                    with pytest.raises(ValueError):
                        cursor_after(encoded, other)
    with pytest.raises(ValueError):
        cursor_after(cursor_for("asr-quality", "draft"), None)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-cursor",
        "é",
        "x" * 513,
        cursor_for("asr-quality", "draft") + "=",
    ],
)
def test_unreadable_cursors_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        cursor_after(value, "draft")


def test_only_the_canonical_cursor_document_is_accepted() -> None:
    def encode(document: dict[str, Any], *, canonical: bool = True) -> str:
        body = json.dumps(
            document,
            sort_keys=canonical,
            separators=(",", ":") if canonical else None,
        ).encode("ascii")
        return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")

    original = {
        "v": 1,
        "resource": "model-binding-list",
        "status": "draft",
        "after": "asr-quality",
    }
    assert cursor_after(encode(original), "draft") == "asr-quality"
    # A cursor is not a signature: a canonical cursor for another identity is a
    # valid position, and it can only move the caller within the same filter.
    assert cursor_after(encode({**original, "after": "asr-other"}), "draft") == "asr-other"
    for changed in (
        {**original, "v": 2},
        {**original, "resource": "workspace-list"},
        {**original, "extra": None},
        {**original, "status": None},
    ):
        with pytest.raises(ValueError):
            cursor_after(encode(changed), "draft")
    # Non-canonical formatting is not something this endpoint ever emitted.
    with pytest.raises(ValueError):
        cursor_after(encode(original, canonical=False), "draft")


def test_list_projects_the_contract_shape_and_passes_the_declared_query() -> None:
    stub = Stub()
    with client(stub) as http:
        response = http.get(
            URL,
            params={"limit": "7", "status": "published"},
            headers={"X-Request-ID": "request-model-bindings"},
        )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "object": "nexus.list",
        "data": [
            {
                "binding_id": "asr-quality",
                "latest_revision": "1",
                "current_revision": None,
                "logical_model": "whisper-large-v3",
                "tasks": ["asr_file"],
                "release_tier": "experimental",
                "publication_status": "draft",
            }
        ],
        "has_more": True,
        "next_cursor": cursor_for("asr-quality", "published"),
    }
    assert len(stub.queries) == 1
    query = stub.queries[0]
    assert (query.limit, query.status, query.after_id) == (7, "published", None)


@pytest.mark.parametrize(
    ("parameters", "code"),
    [
        ({"limit": "0"}, "invalid_model_binding_request"),
        ({"limit": "101"}, "invalid_model_binding_request"),
        ({"limit": "05"}, "invalid_model_binding_request"),
        ({"limit": "1", "extra": "1"}, "invalid_model_binding_request"),
        ({"status": "synthetic"}, "invalid_model_binding_request"),
        ({"cursor": "not-a-cursor"}, "invalid_model_binding_cursor"),
        (
            {"cursor": cursor_for("asr-quality", "draft"), "status": "published"},
            "invalid_model_binding_cursor",
        ),
    ],
)
def test_invalid_query_is_a_400_not_an_unavailable_service(
    parameters: dict[str, str], code: str
) -> None:
    stub = Stub()
    with client(stub) as http:
        response = http.get(URL, params=parameters)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code
    assert stub.queries == []


def test_a_repeated_parameter_or_a_body_is_rejected() -> None:
    stub = Stub()
    with client(stub) as http:
        repeated = http.get(URL + "?limit=1&limit=2")
        body = http.request(
            "GET",
            URL,
            content=b'{"cursor": "x"}',
            headers={"content-type": "application/json"},
        )
        duplicate_id = http.get(URL, headers=[("X-Request-ID", "a"), ("X-Request-ID", "b")])
    assert repeated.status_code == 400
    assert repeated.json()["error"]["code"] == "invalid_model_binding_request"
    assert body.status_code == 400
    assert body.json()["error"]["code"] == "invalid_model_binding_request"
    assert duplicate_id.status_code == 400
    assert stub.queries == []


def test_an_unreadable_registry_is_a_503_with_a_retry_hint() -> None:
    stub = Stub(failure=BindingSummaryUnavailableError("unavailable"))
    with client(stub) as http:
        response = http.get(URL, params={"cursor": cursor_for("raised", None)})
    assert response.status_code == 503, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"] == {
        "code": "model_binding_service_unavailable",
        "type": "service_unavailable_error",
        "message": "Model binding storage is unavailable.",
        "param": None,
        "request_id": response.json()["error"]["request_id"],
        "retryable": False,
    }


def test_a_stale_projection_is_a_503_not_a_partial_list() -> None:
    stub = Stub(failure=ContractValidationError("invalid projection"))
    service = cast(BindingSummaryService, stub)
    with TestClient(create_app(settings=settings(), binding_summaries=service)) as http:
        response = http.get(URL)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_binding_service_unavailable"


def test_rows_sharing_every_visible_attribute_keep_the_keyset_order() -> None:
    # Ordering may not be derived from anything a caller can see: these three
    # rows are identical except for the keyset key, so any order that used
    # logical_model, tasks, release_tier or status would have to fall back to
    # the storage's incidental order.
    rows = tuple(row(binding_id=binding_id) for binding_id in ("asr-a", "asr-b", "asr-c"))
    # The page is what the service emits for limit 2 over those three rows: the
    # two emitted rows plus the key of the last emitted row as the frontier.
    stub = Stub(page=BindingSummaryPage(rows[:2], "asr-b"))
    with client(stub) as http:
        response = http.get(URL, params={"limit": "2"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["binding_id"] for item in body["data"]] == ["asr-a", "asr-b"]
    assert body["has_more"] is True
    assert body["next_cursor"] == cursor_for("asr-b", None)


def test_an_empty_collection_is_an_empty_page_without_a_cursor() -> None:
    stub = Stub(page=BindingSummaryPage((), None))
    with client(stub) as http:
        response = http.get(URL)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "object": "nexus.list",
        "data": [],
        "has_more": False,
        "next_cursor": None,
    }
    assert stub.queries[0].after_id is None


def test_the_last_page_reports_no_more_and_a_caller_can_resume_from_the_cursor() -> None:
    last = Stub(page=BindingSummaryPage((row(binding_id="asr-z"),), None))
    with client(last) as http:
        response = http.get(URL, params={"cursor": cursor_for("asr-y", None)})
    assert response.status_code == 200, response.text
    assert response.json()["has_more"] is False
    assert response.json()["next_cursor"] is None
    # The recorded frontier reached storage unchanged, so the next page starts
    # strictly after the last emitted key instead of repeating it.
    assert last.queries[0].after_id == "asr-y"


@pytest.mark.parametrize("limit", ["1", "50", "100"])
def test_every_declared_limit_is_accepted_and_forwarded(limit: str) -> None:
    stub = Stub(page=BindingSummaryPage((), None))
    with client(stub) as http:
        assert http.get(URL, params={"limit": limit}).status_code == 200
    assert stub.queries[0].limit == int(limit)
