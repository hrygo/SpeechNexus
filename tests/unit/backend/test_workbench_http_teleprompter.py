"""Generated teleprompter HTTP boundaries with no database, model or matcher."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.teleprompter import WorkbenchTeleprompter
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.teleprompter import TeleprompterState
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.interfaces.http_teleprompter import (
    create_teleprompter_router,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
PATH = "/v1/workbench/projects/project-demo/teleprompter"
BODY = {"script_revision": "3", "visible_position": 120, "manual_hold": True}


def state(**changes: Any) -> TeleprompterState:
    values: dict[str, Any] = {
        "project_id": "project-demo",
        "script_revision": 3,
        "visible_position": 120,
        "manual_hold": True,
        "updated_at": NOW,
    }
    values.update(changes)
    return TeleprompterState(**values)


class Store:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failure: BaseException | None = None

    def call(self, **kw: Any) -> None:
        self.calls.append(kw)
        if self.failure:
            raise self.failure

    async def get(self, **kw: Any) -> TeleprompterState:
        self.call(**kw)
        return state()

    async def update(self, **kw: Any) -> TeleprompterState:
        self.call(**kw)
        return state()


def app(store: Any | None) -> FastAPI:
    service = None if store is None else WorkbenchTeleprompter(store)
    value = create_app(
        None,
        teleprompter=service,
        default_workspace_id="workspace-demo",
        deployment_id="synthetic",
        cursor_key=b"synthetic-32-byte-cursor-key-00000",
        max_json_bytes=4096,
        body_timeout_seconds=0.02,
    )
    if store is None:
        value.include_router(
            create_teleprompter_router(
                None,
                default_workspace_id="workspace-demo",
                max_json_bytes=4096,
                body_timeout_seconds=0.02,
            )
        )
    return value


def client(value: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=value), base_url="http://synthetic")


async def test_two_operations_pin_the_server_workspace_and_generated_shape() -> None:
    store = Store()
    async with client(app(store)) as c:
        read = await c.get(PATH)
        assert read.status_code == 200, read.text
        assert read.json() == {
            "object": "nexus.teleprompter_state",
            "project_id": "project-demo",
            "script_revision": "3",
            "visible_position": 120,
            "manual_hold": True,
            "updated_at": "2026-10-24T12:00:00Z",
        }
        write = await c.put(PATH, json=BODY, headers={"Idempotency-Key": "original-key"})
        assert write.status_code == 200, write.text
        assert write.headers["cache-control"] == "no-store" and write.headers["x-request-id"]
        schema = (await c.get("/openapi.json")).json()
        # Both status versions, two voice reads and two public downloads stay mounted.
        assert sum(len(item) for item in schema["paths"].values()) == 20
    assert store.calls[0] == {"project_id": "project-demo", "workspace_id": "workspace-demo"}
    assert store.calls[1] == {
        "project_id": "project-demo",
        "workspace_id": "workspace-demo",
        "script_revision": 3,
        "visible_position": 120,
        "manual_hold": True,
        "key": "original-key",
    }


async def test_unkeyed_update_is_not_invented_a_receipt() -> None:
    store = Store()
    async with client(app(store)) as c:
        assert (await c.put(PATH, json=BODY)).status_code == 200
    assert store.calls[0]["key"] is None


async def test_key_shorter_than_the_declared_header_is_refused_before_the_store() -> None:
    store = Store()
    service = WorkbenchTeleprompter(store)
    with pytest.raises(InvalidVersionChangeError):
        await service.update(
            project_id="project-demo",
            workspace_id="workspace-demo",
            script_revision=3,
            visible_position=120,
            manual_hold=True,
            key="short",
        )
    assert not store.calls


async def test_disabled_router_fails_explicitly_without_phantom_state() -> None:
    async with client(app(None)) as c:
        assert (await c.get(PATH)).status_code == 503
        r = await c.put(PATH, json=BODY)
        assert r.status_code == 503 and r.json()["error"]["code"] == "workbench_unavailable"


@pytest.mark.parametrize(
    "error,status,code,method",
    [
        (ResourceNotFoundError(), 404, "resource_not_found", "get"),
        (ResourceNotFoundError(), 404, "resource_not_found", "put"),
        (IdempotencyConflictError(), 409, "idempotency_conflict", "put"),
        (RevisionConflictError(2, 3), 409, "revision_conflict", "put"),
        (VersionStoreError(sqlstate=None, commit_unknown=True), 503, "outcome_unknown", "put"),
        (VersionStoreError(sqlstate=None, commit_unknown=False), 503, "store_unavailable", "get"),
        (RuntimeError("private"), 500, "internal_error", "get"),
    ],
)
async def test_stable_errors_never_expose_private_exception_text(
    error: BaseException, status: int, code: str, method: str
) -> None:
    store = Store()
    store.failure = error
    async with client(app(store)) as c:
        if method == "get":
            r = await c.get(PATH)
        else:
            r = await c.put(PATH, json=BODY, headers={"Idempotency-Key": "original-key"})
        assert r.status_code == status and r.json()["error"]["code"] == code, r.text
        assert "private" not in r.text and r.headers["x-should-retry"] == "false"


@pytest.mark.parametrize(
    "payload",
    [
        {"visible_position": 0, "manual_hold": False},
        {"script_revision": 3, "visible_position": 0, "manual_hold": False},
        {"script_revision": "0", "visible_position": 0, "manual_hold": False},
        {"script_revision": "3", "visible_position": -1, "manual_hold": False},
        {"script_revision": "3", "visible_position": 0, "manual_hold": "false"},
        {
            "script_revision": "3",
            "visible_position": 0,
            "manual_hold": False,
            "stream_epoch": "synthetic-private",
        },
    ],
)
async def test_contract_rejects_a_body_before_the_store(payload: dict[str, Any]) -> None:
    store = Store()
    async with client(app(store)) as c:
        r = await c.put(PATH, json=payload)
        assert r.status_code == 400, r.text
        assert not store.calls
        assert (await c.get(PATH + "?workspace_id=other")).status_code == 400
        assert not store.calls


async def test_position_beyond_the_representable_bound_is_a_declared_change_failure() -> None:
    store = Store()
    async with client(app(store)) as c:
        r = await c.put(PATH, json={**BODY, "visible_position": 2**53})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_change", r.text
        assert "private" not in r.text
    # The application boundary refuses it, so no store command is attempted.
    assert not store.calls


async def test_body_deadline_and_cancellation_are_not_successes() -> None:
    store = Store()

    async def slow() -> Any:
        yield b"{"
        await asyncio.sleep(0.05)
        yield b"}"

    async with client(app(store)) as c:
        r = await c.put(PATH, content=slow(), headers={"Content-Type": "application/json"})
        assert r.status_code == 408 and not store.calls
        store.failure = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await c.get(PATH)
