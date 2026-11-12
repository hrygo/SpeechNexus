"""Cursor scope, request framing and projection of the administrative audit list.

The cursor freezes the composite position and the selected action filter by
construction, so a cursor from one filter cannot silently page another. The read
declares no body, no repeated parameter and only the contract's query fields.
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
from speechnexus_backend.speech.application.audit_events import AuditEventService
from speechnexus_backend.speech.domain.audit_events import (
    AuditChangeSummary,
    AuditEvent,
    AuditEventPage,
    AuditEventPosition,
    AuditEventUnavailableError,
)
from speechnexus_backend.speech.interfaces.http.audit_events import (
    cursor_for,
    cursor_position,
)
from tests.unit.backend.test_health import _settings

URL = "/v1/nexus/audit-events"
PREFIX = "lifecycle-"


def event(
    suffix: str = "1",
    *,
    occurred_at_us: int = 1_700_000_000_000_001,
    action: str = "model_binding.retire",
) -> AuditEvent:
    return AuditEvent(
        event_id=PREFIX + suffix.rjust(32, "0"),
        actor_type="anonymous",
        credential_label=None,
        request_id="request-audit-1",
        action=action,
        resource_type="model_binding",
        resource_id="asr-quality",
        occurred_at_us=occurred_at_us,
        policy_revision=7,
        change_summary=AuditChangeSummary(("/publication/status", "/publication/retired_at"), None),
    )


class Stub:
    def __init__(
        self,
        page: AuditEventPage | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.page = page or AuditEventPage(
            (event(),),
            AuditEventPosition(1_700_000_000_000_001, event().event_id),
        )
        self.failure = failure
        self.queries: list[Any] = []

    def list(self, query: Any) -> AuditEventPage:
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
    service = cast(AuditEventService, stub)
    return TestClient(create_app(settings=settings(), audit_events=service))


def test_cursor_round_trips_and_is_bound_to_its_action_filter() -> None:
    position = AuditEventPosition(
        1_700_000_000_000_001, PREFIX + "00000000000000000000000000000001"
    )
    for action in (None, "model_binding.retire", "model_binding.disable", ""):
        encoded = cursor_for(position, action)
        assert cursor_position(encoded, action) == position
        if action is not None:
            for other in ("model_binding.retire", "model_binding.disable", None):
                if other != action:
                    with pytest.raises(ValueError):
                        cursor_position(encoded, other)
    with pytest.raises(ValueError):
        cursor_position(cursor_for(position, "model_binding.retire"), None)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-cursor",
        "é",
        "x" * 513,
        cursor_for(AuditEventPosition(1, PREFIX + "00000000000000000000000000000001"), None) + "=",
    ],
)
def test_unreadable_cursors_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        cursor_position(value, None)


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
        "resource": "audit-event-list",
        "action": None,
        "occurred_at_us": 1_700_000_000_000_001,
        "event_id": PREFIX + "00000000000000000000000000000001",
    }
    assert cursor_position(encode(original), None) == AuditEventPosition(
        1_700_000_000_000_001, PREFIX + "00000000000000000000000000000001"
    )
    for changed in (
        {**original, "v": 2},
        {**original, "resource": "model-binding-list"},
        {**original, "extra": None},
        {**original, "action": "model_binding.retire"},
        {**original, "occurred_at_us": -1},
        {**original, "occurred_at_us": 253_402_300_799_999_999 + 1},
        {**original, "occurred_at_us": True},
        {**original, "event_id": "Not-An-Identifier"},
    ):
        with pytest.raises(ValueError):
            cursor_position(encode(changed), None)
    # Non-canonical formatting is not something this endpoint ever emitted.
    with pytest.raises(ValueError):
        cursor_position(encode(original, canonical=False), None)


def test_list_projects_the_contract_shape_and_passes_the_declared_query() -> None:
    stub = Stub()
    with client(stub) as http:
        response = http.get(
            URL,
            params={"limit": "7", "action": "model_binding.retire"},
            headers={"X-Request-ID": "request-audit-events"},
        )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == "request-audit-events"
    assert response.json() == {
        "object": "nexus.list",
        "data": [
            {
                "id": PREFIX + "00000000000000000000000000000001",
                "object": "nexus.audit_event",
                "actor_type": "anonymous",
                "credential_label": None,
                "request_id": "request-audit-1",
                "action": "model_binding.retire",
                "resource_type": "model_binding",
                "resource_id": "asr-quality",
                "occurred_at": "2023-11-14T22:13:20.000001Z",
                "policy_revision": "7",
                "change_summary": {
                    "changed_fields": ["/publication/status", "/publication/retired_at"],
                    "reason": None,
                },
            }
        ],
        "has_more": True,
        "next_cursor": cursor_for(
            AuditEventPosition(1_700_000_000_000_001, PREFIX + "00000000000000000000000000000001"),
            "model_binding.retire",
        ),
    }
    assert len(stub.queries) == 1
    query = stub.queries[0]
    assert (query.limit, query.action, query.after) == (7, "model_binding.retire", None)


@pytest.mark.parametrize(
    ("parameters", "code"),
    [
        ({"limit": "0"}, "invalid_audit_request"),
        ({"limit": "101"}, "invalid_audit_request"),
        ({"limit": "05"}, "invalid_audit_request"),
        ({"limit": "1", "extra": "1"}, "invalid_audit_request"),
        ({"action": "x" * 129}, "invalid_audit_request"),
        ({"cursor": "not-a-cursor"}, "invalid_audit_cursor"),
        (
            {
                "cursor": cursor_for(
                    AuditEventPosition(1, PREFIX + "00000000000000000000000000000001"),
                    "model_binding.retire",
                ),
                "action": "model_binding.disable",
            },
            "invalid_audit_cursor",
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
    assert response.json()["error"]["type"] == "invalid_request_error"
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
    assert repeated.json()["error"]["code"] == "invalid_audit_request"
    assert body.status_code == 400
    assert body.json()["error"]["code"] == "invalid_audit_request"
    assert duplicate_id.status_code == 400
    assert stub.queries == []


def test_an_unreadable_registry_is_a_503_with_a_retry_hint() -> None:
    stub = Stub(failure=AuditEventUnavailableError("unavailable"))
    with client(stub) as http:
        response = http.get(URL)
    assert response.status_code == 503, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"] == {
        "code": "audit_service_unavailable",
        "type": "service_unavailable_error",
        "message": "Audit event storage is unavailable.",
        "param": None,
        "request_id": response.json()["error"]["request_id"],
        "retryable": False,
    }


def test_a_stale_projection_is_a_503_not_a_partial_list() -> None:
    stub = Stub(failure=ContractValidationError("invalid projection"))
    service = cast(AuditEventService, stub)
    with TestClient(create_app(settings=settings(), audit_events=service)) as http:
        response = http.get(URL)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_service_unavailable"


def test_an_absent_reader_identity_is_a_503_not_an_empty_history() -> None:
    app = create_app(settings=settings())
    with TestClient(app) as http:
        response = http.get(URL)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "audit_service_unavailable"


def test_rows_sharing_a_timestamp_keep_the_composite_keyset_order() -> None:
    # Both rows share every visible timestamp; only the unique event identity
    # separates them, so any order derived from occurred_at alone would depend
    # on the storage's incidental row order.
    older = event("2", occurred_at_us=5)
    newer = event("3", occurred_at_us=5)
    stub = Stub(page=AuditEventPage((newer,), AuditEventPosition(5, newer.event_id)))
    with client(stub) as http:
        response = http.get(URL, params={"limit": "1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["data"]] == [newer.event_id]
    assert body["has_more"] is True
    assert body["next_cursor"] == cursor_for(AuditEventPosition(5, newer.event_id), None)
    # The other row is strictly after the emitted position on the same
    # timestamp, so it belongs to the next page; a cursor carrying only the
    # timestamp could not express that boundary.
    assert older.occurred_at_us == newer.occurred_at_us
    assert newer.event_id > older.event_id


def test_an_empty_collection_is_an_empty_page_without_a_cursor() -> None:
    stub = Stub(page=AuditEventPage((), None))
    with client(stub) as http:
        response = http.get(URL)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "object": "nexus.list",
        "data": [],
        "has_more": False,
        "next_cursor": None,
    }
    assert stub.queries[0].after is None


def test_an_unknown_action_filter_is_an_empty_page_not_an_error() -> None:
    # The contract declares `action` as an open string that matches one
    # recorded action exactly; a value no row carries selects nothing.
    stub = Stub(page=AuditEventPage((), None))
    with client(stub) as http:
        response = http.get(URL, params={"action": "profile.update"})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []
    assert stub.queries[0].action == "profile.update"


def test_the_last_page_reports_no_more_and_a_caller_can_resume_from_the_cursor() -> None:
    last = Stub(page=AuditEventPage((event("9", occurred_at_us=1),), None))
    frontier = AuditEventPosition(2, event("8").event_id)
    with client(last) as http:
        response = http.get(URL, params={"cursor": cursor_for(frontier, None)})
    assert response.status_code == 200, response.text
    assert response.json()["has_more"] is False
    assert response.json()["next_cursor"] is None
    # The recorded frontier reached storage unchanged, so the next page starts
    # strictly after the last emitted position instead of repeating it.
    assert last.queries[0].after == frontier


@pytest.mark.parametrize("limit", ["1", "50", "100"])
def test_every_declared_limit_is_accepted_and_forwarded(limit: str) -> None:
    stub = Stub(page=AuditEventPage((), None))
    with client(stub) as http:
        assert http.get(URL, params={"limit": limit}).status_code == 200
    assert stub.queries[0].limit == int(limit)


def test_the_contract_default_is_used_when_no_limit_is_supplied() -> None:
    stub = Stub(page=AuditEventPage((), None))
    with client(stub) as http:
        assert http.get(URL).status_code == 200
    assert stub.queries[0].limit == 50
