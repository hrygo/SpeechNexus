"""Read rules and real HTTP boundaries; PG facts are qualified separately."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from openapi_spec_validator import validate
from speechnexus_contracts.admin_http import contract, validate_response

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.workspaces import WorkspaceReads, WorkspaceService
from speechnexus_backend.speech.domain.workspaces import (
    Workspace,
    WorkspaceNotFoundError,
    WorkspaceQuery,
    WorkspaceUnavailableError,
)
from speechnexus_backend.speech.interfaces.http.workspaces import cursor_after, cursor_for
from tests.unit.backend.test_health import _settings

ROW = Workspace("workspace-demo", "Synthetic group", "active", 3, 2**53 + 1, 1)
PREFIX = "/v1/nexus/workspaces"


def setup() -> tuple[Any, Mock]:
    reads = Mock(spec=WorkspaceReads)
    reads.get.return_value = ROW
    reads.scan.return_value = (ROW,)
    settings = _settings().model_copy(update={"asset_allowed_hosts": ("test",)})
    return create_app(settings=settings, workspaces=WorkspaceService(reads)), reads


@pytest.mark.parametrize(
    "change",
    [
        {"workspace_id": "../private"},
        {"display_name": ""},
        {"display_name": "x" * 129},
        {"status": "ready"},
        {"metadata_revision": True},
        {"metadata_revision": 0},
        {"metadata_revision": 2**53},
        {"policy_revision": 0},
        {"policy_revision": 2**63},
        {"created_at_us": -1},
        {"created_at_us": True},
    ],
)
def test_domain_rejects_unrepresentable_authority(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(ROW, **change)


def test_service_never_invents_default_or_accepts_wrong_or_unsorted_records() -> None:
    reads = Mock(spec=WorkspaceReads)
    service = WorkspaceService(reads)
    reads.get.return_value = None
    with pytest.raises(WorkspaceNotFoundError):
        service.get("workspace-default")
    reads.get.return_value = ROW
    with pytest.raises(WorkspaceUnavailableError):
        service.get("workspace-other")
    for rows in [(ROW, ROW), (ROW, replace(ROW, workspace_id="workspace-aaa")), (ROW,) * 3]:
        reads.scan.return_value = rows
        with pytest.raises(WorkspaceUnavailableError):
            service.list(WorkspaceQuery(1))
    reads.scan.return_value = (ROW,)
    with pytest.raises(WorkspaceUnavailableError):
        service.list(WorkspaceQuery(1, ROW.workspace_id))


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_query_requires_explicit_bounded_page_size(limit: Any) -> None:
    with pytest.raises(ValueError):
        WorkspaceQuery(limit)


@pytest.mark.parametrize(
    "value", ["", "not-a-cursor", cursor_for(ROW.workspace_id) + "=", "é", "x" * 513]
)
def test_cursor_requires_bounded_canonical_encoding(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid workspace cursor"):
        cursor_after(value)


@pytest.mark.parametrize(
    "change",
    [{"v": True}, {"v": 2}, {"resource": "assets"}, {"after": "../private"}, {"private": True}],
)
def test_cursor_cannot_select_another_resource_or_unknown_shape(change: dict[str, Any]) -> None:
    wire = {"v": 1, "resource": "workspace-list", "after": ROW.workspace_id} | change
    encoded = (
        base64.urlsafe_b64encode(json.dumps(wire, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(ValueError):
        cursor_after(encoded)


async def test_http_projects_actual_record_and_source_default_without_private_fields() -> None:
    app, reads = setup()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX, headers={"X-Request-ID": "synthetic-workspace"})
        assert response.status_code == 200
        validate_response("listWorkspaces", 200, {"body": response.json()})
        reads.scan.assert_called_once_with(
            WorkspaceQuery(contract()["components"]["parameters"]["Limit"]["schema"]["default"])
        )
        value = response.json()
        assert value["data"][0]["policy_revision"] == str(2**53 + 1)
        assert value["data"][0]["created_at"] == "1970-01-01T00:00:00.000001Z"
        assert value["next_cursor"] is None and value["has_more"] is False
        assert response.headers["Cache-Control"] == "no-store"
        reads.get.return_value = replace(ROW, status="disabled")
        single = await client.get(PREFIX + "/" + ROW.workspace_id)
        assert single.status_code == 200 and single.json()["status"] == "disabled"
        assert "workspace_state" not in single.text


async def test_http_pages_using_original_last_key_without_inserting_defaults() -> None:
    app, reads = setup()
    second = replace(ROW, workspace_id="workspace-zzz")
    reads.scan.return_value = (ROW, second)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = (await client.get(PREFIX, params={"limit": 1})).json()
        assert first["data"][0]["id"] == ROW.workspace_id
        assert first["has_more"] is True
        assert cursor_after(first["next_cursor"]) == ROW.workspace_id
        reads.scan.return_value = (second,)
        last = await client.get(PREFIX, params={"limit": 1, "cursor": first["next_cursor"]})
        reads.scan.assert_called_with(WorkspaceQuery(1, ROW.workspace_id))
        assert last.json()["data"][0]["id"] == second.workspace_id
        assert last.json()["next_cursor"] is None


@pytest.mark.parametrize(
    "suffix,headers,content,status",
    [
        ("?limit=0", {}, None, 400),
        ("?limit=101", {}, None, 400),
        ("?limit=01", {}, None, 400),
        ("?limit=1&limit=2", {}, None, 400),
        ("?unknown=private", {}, None, 400),
        ("?cursor=invalid", {}, None, 400),
        ("", {"X-Request-ID": ""}, None, 400),
        ("", [("X-Request-ID", "one"), ("x-request-id", "two")], None, 400),
        ("", {"Host": "unapproved.invalid"}, None, 403),
        ("", {"Host": "@test"}, None, 403),
        ("", {"Host": ":@test"}, None, 403),
        ("", {"Host": "test?"}, None, 403),
        ("", {"Host": "test#"}, None, 403),
        ("", {"Origin": "https://unapproved.invalid"}, None, 403),
        ("", [("Host", "test"), ("Host", "test")], None, 403),
        ("", {}, b"private body", 400),
        ("/INVALID", {}, None, 400),
    ],
)
async def test_invalid_transport_never_calls_authority(
    suffix: str, headers: Any, content: bytes | None, status: int
) -> None:
    app, reads = setup()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request("GET", PREFIX + suffix, headers=headers, content=content)
    assert response.status_code == status
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    assert response.json()["error"]["retryable"] is False
    reads.get.assert_not_called()
    reads.scan.assert_not_called()


async def test_missing_unavailable_and_unimplemented_methods_have_fixed_public_errors() -> None:
    app, reads = setup()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reads.get.return_value = None
        missing = await client.get(PREFIX + "/workspace-missing")
        assert missing.status_code == 404
        reads.scan.side_effect = WorkspaceUnavailableError("postgres://private/credentials")
        failure = await client.get(PREFIX)
        assert failure.status_code == 503 and "private" not in failure.text
        for method in ("POST", "PATCH", "DELETE", "HEAD"):
            unsupported = await client.request(method, PREFIX)
            assert unsupported.status_code == 405
            assert "GET" in unsupported.headers["Allow"]
    cold = create_app(settings=_settings())
    async with (
        cold.router.lifespan_context(cold),
        AsyncClient(transport=ASGITransport(app=cold), base_url="http://test") as client,
    ):
        assert (await client.get(PREFIX)).status_code == 503
        assert (await client.get("/healthz")).status_code == 200


def test_openapi_advertises_only_real_gets_and_preserves_asset_schema_names() -> None:
    app, _ = setup()
    document = app.openapi()
    validate(document)
    for path in (PREFIX, PREFIX + "/{workspace_id}"):
        assert set(document["paths"][path]) == {"get"}
        assert document["paths"][path]["get"]["security"] == [
            {"WorkspaceRead_SharedServiceKey": []},
            {},
        ]
    assert "Asset" in document["components"]["schemas"]
    assert "WorkspaceRead_Workspace" in document["components"]["schemas"]
    assert app.openapi() == document
