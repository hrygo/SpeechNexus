"""Read and write rules plus real HTTP boundaries; PG facts are qualified separately.

``Port`` below is an in-memory write port: it proves the boundary maps the port's
decisions onto declared responses, not that PostgreSQL enforces one row per key.
The latter is proved in ``tests/integration/test_workspace_writes.py``.
"""

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
from speechnexus_backend.speech.application.workspaces import (
    WorkspaceReads,
    WorkspaceService,
)
from speechnexus_backend.speech.domain.workspaces import (
    Workspace,
    WorkspaceConflictError,
    WorkspaceCreateCommand,
    WorkspaceNotFoundError,
    WorkspaceQuery,
    WorkspaceUnavailableError,
    WorkspaceUpdateCommand,
    WorkspaceWriteReceipt,
)
from speechnexus_backend.speech.interfaces.http.workspaces import cursor_after, cursor_for
from tests.unit.backend.test_health import _settings

ROW = Workspace("workspace-demo", "Synthetic group", "active", 3, 2**53 + 1, 1)
FIXED = "workspace-allocated"
PREFIX = "/v1/nexus/workspaces"


class Port:
    """One recorded result per key; the same decisions the SQL port must make."""

    def __init__(self) -> None:
        self.rows: dict[str, Workspace] = {}
        self.keys: dict[str, tuple[str, str, Workspace]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.failure: Exception | None = None

    def create(
        self, command: WorkspaceCreateCommand, *, workspace_id: str
    ) -> WorkspaceWriteReceipt:
        self.calls.append(("create", command.intent_digest, workspace_id))
        if self.failure is not None:
            raise self.failure
        if command.key_digest is not None and command.key_digest in self.keys:
            original = self.keys[command.key_digest]
            if original[:2] != ("create", command.intent_digest):
                raise WorkspaceConflictError("idempotency_conflict")
            return WorkspaceWriteReceipt(self.rows[original[2].workspace_id], True)
        if workspace_id in self.rows:
            raise WorkspaceConflictError("workspace_identity_conflict")
        record = Workspace(
            workspace_id, command.display_name, "active", 1, 1, 1_700_000_000_000_000
        )
        self.rows[workspace_id] = record
        if command.key_digest is not None:
            self.keys[command.key_digest] = ("create", command.intent_digest, record)
        return WorkspaceWriteReceipt(record, False)

    def update(self, command: WorkspaceUpdateCommand) -> WorkspaceWriteReceipt:
        self.calls.append(("update", command.intent_digest, command.workspace_id))
        if self.failure is not None:
            raise self.failure
        current = self.rows.get(command.workspace_id)
        if current is None:
            raise WorkspaceNotFoundError
        if command.key_digest is not None and command.key_digest in self.keys:
            original = self.keys[command.key_digest]
            if original[:2] != ("update", command.intent_digest) or (
                original[2].workspace_id != command.workspace_id
            ):
                raise WorkspaceConflictError("idempotency_conflict")
            return WorkspaceWriteReceipt(self.rows[current.workspace_id], True)
        if current.metadata_revision != command.expected_metadata_revision:
            raise WorkspaceConflictError("configuration_changed")
        record = replace(
            current,
            display_name=command.display_name or current.display_name,
            status=command.status or current.status,
            metadata_revision=current.metadata_revision + 1,
        )
        self.rows[command.workspace_id] = record
        if command.key_digest is not None:
            self.keys[command.key_digest] = ("update", command.intent_digest, record)
        return WorkspaceWriteReceipt(record, False)


def setup(writes: Port | None = None) -> tuple[Any, Mock, Port | None]:
    reads = Mock(spec=WorkspaceReads)
    reads.get.return_value = ROW
    reads.scan.return_value = (ROW,)
    settings = _settings().model_copy(update={"asset_allowed_hosts": ("test",)})
    service = WorkspaceService(reads, writes, allocate=lambda: FIXED)
    return create_app(settings=settings, workspaces=service), reads, writes


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
    app, reads, _ = setup()
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
    app, reads, _ = setup()
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
    app, reads, _ = setup()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request("GET", PREFIX + suffix, headers=headers, content=content)
    assert response.status_code == status
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    assert response.json()["error"]["retryable"] is False
    reads.get.assert_not_called()
    reads.scan.assert_not_called()


async def test_missing_unavailable_and_unimplemented_methods_have_fixed_public_errors() -> None:
    app, reads, _ = setup()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reads.get.return_value = None
        missing = await client.get(PREFIX + "/workspace-missing")
        assert missing.status_code == 404
        reads.scan.side_effect = WorkspaceUnavailableError("postgres://private/credentials")
        failure = await client.get(PREFIX)
        assert failure.status_code == 503 and "private" not in failure.text
        for method in ("PUT", "DELETE", "HEAD"):
            unsupported = await client.request(method, PREFIX)
            assert unsupported.status_code == 405
            assert "GET" in unsupported.headers["Allow"] and "POST" in unsupported.headers["Allow"]
        for method in ("POST", "PUT", "DELETE"):
            unsupported = await client.request(method, PREFIX + "/" + ROW.workspace_id)
            assert unsupported.status_code == 405
            assert "PATCH" in unsupported.headers["Allow"]
    cold = create_app(settings=_settings())
    async with (
        cold.router.lifespan_context(cold),
        AsyncClient(transport=ASGITransport(app=cold), base_url="http://test") as client,
    ):
        assert (await client.get(PREFIX)).status_code == 503
        created = await client.post(PREFIX + "?x=1", json={"display_name": "Synthetic"})
        assert created.status_code == 503
        validate_response("createWorkspace", 503, {"body": created.json()})
        assert (await client.get("/healthz")).status_code == 200


async def test_same_key_twice_reports_the_same_identity_and_one_ledger_entry() -> None:
    port = Port()
    app, _, _ = setup(port)
    headers = {"Idempotency-Key": "workspace-create-key-1"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(PREFIX, json={"display_name": "共享分组"}, headers=headers)
        second = await client.post(PREFIX, json={"display_name": "共享分组"}, headers=headers)
    assert first.status_code == second.status_code == 201
    first_body, second_body = first.json(), second.json()
    validate_response("createWorkspace", 201, {"body": first_body})
    validate_response("createWorkspace", 201, {"body": second_body})
    assert first_body == second_body and first_body["id"] == FIXED
    assert first_body["metadata_revision"] == 1
    assert len(port.rows) == 1 and len(port.keys) == 1
    assert [call[0] for call in port.calls] == ["create", "create"]


async def test_same_key_with_a_different_body_is_a_conflict_and_writes_nothing_more() -> None:
    port = Port()
    app, _, _ = setup(port)
    headers = {"Idempotency-Key": "workspace-create-key-2"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post(PREFIX, json={"display_name": "First"}, headers=headers)
        ).status_code == 201
        conflict = await client.post(PREFIX, json={"display_name": "Second"}, headers=headers)
    assert conflict.status_code == 409
    validate_response("createWorkspace", 409, {"body": conflict.json()})
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert len(port.rows) == 1 and len(port.keys) == 1


async def test_create_requires_canonical_json_and_rejects_the_unsupported_policy() -> None:
    port = Port()
    app, _, _ = setup(port)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        policy = await client.post(
            PREFIX, json={"display_name": "共享分组", "policy_id": "policy-quota-r1"}
        )
        assert policy.status_code == 422
        validate_response("createWorkspace", 422, {"body": policy.json()})
        assert policy.json()["error"]["param"] == "policy_id"
        assert policy.json()["error"]["code"] == "workspace_policy_unsupported"
        unknown = await client.post(PREFIX, json={"display_name": "x", "workspace_id": "mine"})
        assert unknown.status_code == 400
        validate_response("createWorkspace", 400, {"body": unknown.json()})
        assert (
            await client.post(
                PREFIX, content=b'{"display_name": "x"}', headers={"Content-Type": "text/plain"}
            )
        ).status_code == 400
        assert (
            await client.post(PREFIX, content=b'{"display_name": "a", "display_name": "b"}')
        ).status_code == 400
        assert (
            await client.post(PREFIX, content=b'{"display_name": "x" * 4096}')
        ).status_code == 400
        assert (await client.post(PREFIX)).status_code == 400
        assert port.calls == []


@pytest.mark.parametrize("headers", [{"Idempotency-Key": "short"}, {"Idempotency-Key": "x" * 129}])
async def test_idempotency_key_bounds_follow_the_shared_contract(headers: dict[str, str]) -> None:
    app, _, _ = setup(Port())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PREFIX, json={"display_name": "x"}, headers=headers)
    assert response.status_code == 400
    validate_response("createWorkspace", 400, {"body": response.json()})


async def test_update_applies_the_patch_and_replays_without_a_second_revision() -> None:
    port = Port()
    app, _, _ = setup(port)
    body = {"expected_metadata_revision": 1, "display_name": "Renamed", "status": "disabled"}
    headers = {"Idempotency-Key": "workspace-update-key-1"}
    target = PREFIX + "/" + FIXED
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(PREFIX, json={"display_name": "First"})).status_code == 201
        first = await client.patch(target, json=body, headers=headers)
        second = await client.patch(target, json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    validate_response("updateWorkspace", 200, {"body": first.json()})
    assert first.json() == second.json()
    assert first.json()["display_name"] == "Renamed" and first.json()["status"] == "disabled"
    assert first.json()["metadata_revision"] == 2
    assert len(port.keys) == 1


async def test_update_conflicts_and_missing_targets_map_to_declared_errors() -> None:
    port = Port()
    app, _, _ = setup(port)
    target = PREFIX + "/" + FIXED
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(PREFIX, json={"display_name": "First"})).status_code == 201
        stale = await client.patch(
            target, json={"expected_metadata_revision": 9, "status": "active"}
        )
        assert stale.status_code == 409
        validate_response("updateWorkspace", 409, {"body": stale.json()})
        assert stale.json()["error"]["code"] == "configuration_changed"
        assert port.rows[FIXED].metadata_revision == 1
        port.failure = WorkspaceNotFoundError()
        missing = await client.patch(
            target, json={"expected_metadata_revision": 1, "status": "active"}
        )
        assert missing.status_code == 404
        validate_response("updateWorkspace", 404, {"body": missing.json()})
        port.failure = WorkspaceUnavailableError("postgres://private/credentials")
        unavailable = await client.patch(
            target, json={"expected_metadata_revision": 1, "status": "active"}
        )
        assert unavailable.status_code == 503 and "private" not in unavailable.text
        validate_response("updateWorkspace", 503, {"body": unavailable.json()})


async def test_update_requires_a_canonical_patch_and_a_writable_target() -> None:
    port = Port()
    app, _, _ = setup(port)
    target = PREFIX + "/workspace-missing"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.patch(target, json={"expected_metadata_revision": 1})
        ).status_code == 400
        assert (
            await client.patch(
                target, json={"expected_metadata_revision": 1, "policy_id": "policy-quota-r1"}
            )
        ).status_code == 400
        assert (
            await client.patch(target, json={"expected_metadata_revision": 0, "status": "active"})
        ).status_code == 400
        assert (
            await client.patch(target, json={"expected_metadata_revision": 1, "status": "ready"})
        ).status_code == 400
        assert (
            await client.patch(
                target + "?limit=1", json={"expected_metadata_revision": 1, "status": "active"}
            )
        ).status_code == 400
        assert port.calls == []


async def test_writes_without_a_configured_writer_report_unavailable() -> None:
    app, _, _ = setup()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(PREFIX, json={"display_name": "Shared"})
        patched = await client.patch(
            PREFIX + "/" + ROW.workspace_id,
            json={"expected_metadata_revision": 1, "status": "disabled"},
        )
    for response, operation, status in (
        (created, "createWorkspace", 503),
        (patched, "updateWorkspace", 503),
    ):
        assert response.status_code == status
        validate_response(operation, status, {"body": response.json()})
        assert "private" not in response.text
        assert response.headers["Cache-Control"] == "no-store"


def test_openapi_advertises_the_reads_and_writes_and_keeps_asset_schema_names() -> None:
    app, _, _ = setup()
    document = app.openapi()
    validate(document)
    assert set(document["paths"][PREFIX]) == {"get", "post"}
    assert set(document["paths"][PREFIX + "/{workspace_id}"]) == {"get", "patch"}
    for path in (PREFIX, PREFIX + "/{workspace_id}"):
        for method, operation in document["paths"][path].items():
            assert operation["security"] == [{"WorkspaceAdmin_SharedServiceKey": []}, {}], method
    assert document["paths"][PREFIX]["post"]["operationId"] == "createWorkspace"
    assert (
        document["paths"][PREFIX + "/{workspace_id}"]["patch"]["operationId"] == "updateWorkspace"
    )
    assert "Asset" in document["components"]["schemas"]
    assert "WorkspaceAdmin_Workspace" in document["components"]["schemas"]
    assert "WorkspaceAdmin_WorkspaceCreateRequest" in document["components"]["schemas"]
    assert app.openapi() == document
