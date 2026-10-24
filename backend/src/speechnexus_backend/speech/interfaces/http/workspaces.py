"""Real shared-group reads; canonical DTO validation stays at the HTTP boundary."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin import parse
from speechnexus_contracts.admin_http import contract, validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from speechnexus_backend.speech.application.workspaces import WorkspaceService
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.workspaces import (
    Workspace,
    WorkspaceNotFoundError,
    WorkspaceQuery,
    WorkspaceUnavailableError,
)
from speechnexus_backend.speech.interfaces.http.origin_policy import rejected_authority

PREFIX = "/v1/nexus/workspaces"
_ERRORS = {
    "invalid_workspace_request": (400, "invalid_request_error", "Invalid workspace request."),
    "invalid_workspace_cursor": (400, "invalid_request_error", "Invalid workspace cursor."),
    "workspace_host_rejected": (403, "permission_error", "The workspace Host is not allowed."),
    "workspace_origin_rejected": (403, "permission_error", "The workspace Origin is not allowed."),
    "workspace_not_found": (404, "not_found_error", "The workspace was not found."),
    "workspace_method_not_allowed": (
        405,
        "invalid_request_error",
        "The workspace method is not allowed.",
    ),
    "workspace_service_unavailable": (
        503,
        "service_unavailable_error",
        "Workspace storage is unavailable.",
    ),
}


def workspace_error(
    code: str, request_id: str, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    if code not in _ERRORS:
        code = "workspace_service_unavailable"
    status, kind, message = _ERRORS[code]
    return JSONResponse(
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Should-Retry": "false", **(headers or {})},
        content=parse(
            "ErrorResponse",
            {
                "error": {
                    "code": code,
                    "type": kind,
                    "message": message,
                    "param": None,
                    "request_id": request_id,
                    "retryable": False,
                }
            },
        ),
    )


def cursor_for(after_id: str) -> str:
    identifier(after_id)
    body = json.dumps(
        {"v": 1, "resource": "workspace-list", "after": after_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def cursor_after(value: str) -> str:
    try:
        if not 1 <= len(value) <= 512:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {"v", "resource", "after"}:
            raise ValueError
        if (
            type(document["v"]) is not int
            or document["v"] != 1
            or document["resource"] != "workspace-list"
        ):
            raise ValueError
        after = identifier(document["after"])
        if cursor_for(after) != value:
            raise ValueError
        return after
    except ValueError, TypeError, KeyError, binascii.Error:
        raise ValueError("Invalid workspace cursor") from None


def public_workspace(record: Workspace) -> dict[str, Any]:
    stamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=record.created_at_us)
    return dict(
        parse(
            "Workspace",
            {
                "id": record.workspace_id,
                "object": "nexus.workspace",
                "display_name": record.display_name,
                "status": record.status,
                "metadata_revision": record.metadata_revision,
                "policy_revision": str(record.policy_revision),
                "created_at": stamp.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            },
        )
    )


class WorkspaceIngress:
    def __init__(
        self,
        app: ASGIApp,
        *,
        is_enabled: Callable[[], bool],
        allowed_hosts: tuple[str, ...],
        allowed_origins: tuple[str, ...],
    ) -> None:
        self._app, self._enabled = app, is_enabled
        self._hosts, self._origins = frozenset(allowed_hosts), frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"].removeprefix(scope.get("root_path", ""))
        if not (path == PREFIX or path.startswith(PREFIX + "/")):
            await self._app(scope, receive, send)
            return
        request_id = scope["state"]["request_id"]
        if not self._enabled():
            await workspace_error("workspace_service_unavailable", request_id)(scope, receive, send)
            return
        rejection = rejected_authority(Headers(scope=scope), self._hosts, self._origins)
        if rejection is not None:
            await workspace_error("workspace_" + rejection + "_rejected", request_id)(
                scope, receive, send
            )
            return
        await self._app(scope, receive, send)


def create_workspace_router(get_service: Callable[[], WorkspaceService | None]) -> APIRouter:
    router = APIRouter(tags=["workspaces"])
    # Deployment defaults are selected by the authoritative source, not copied
    # into a second HTTP/query DTO or silently inserted into a client's input.
    default_limit = contract()["components"]["parameters"]["Limit"]["schema"]["default"]
    if type(default_limit) is not int:
        raise ValueError("Workspace limit default must be an integer")

    async def read(request: Request, *, single: bool) -> Response:
        request_id = str(request.state.request_id)
        service = get_service()
        if service is None:
            return workspace_error("workspace_service_unavailable", request_id)
        operation = "getWorkspace" if single else "listWorkspaces"
        if len(request.scope.get("query_string", b"")) > 4096:
            return workspace_error("invalid_workspace_request", request_id)
        pairs = list(request.query_params.multi_items())
        ids = request.headers.getlist("x-request-id")
        lengths = request.headers.getlist("content-length")
        try:
            if len(pairs) != len({name for name, _ in pairs}) or len(ids) > 1:
                raise ContractValidationError("Duplicate workspace parameter")
            if (
                len(lengths) > 1
                or (lengths and lengths[0] != "0")
                or request.headers.getlist("transfer-encoding")
            ):
                raise ContractValidationError("A workspace read has no body")
            # Empty ASGI frames may precede EOF. Bound both frames and time,
            # rejecting any bytes without allocating a JSON/body buffer.
            async with asyncio.timeout(1):
                for _ in range(8):
                    message = await request.receive()
                    if message["type"] != "http.request" or message.get("body"):
                        raise ContractValidationError("A workspace read has no body")
                    if not message.get("more_body", False):
                        break
                else:
                    raise ContractValidationError("Workspace receive budget exceeded")
            query = dict(pairs)
            envelope = {
                "path": request.path_params,
                "query": query,
                "headers": {"x-request-id": ids[0]} if ids else {},
            }
            validate_request(operation, envelope)
        except ContractValidationError, TimeoutError, ClientDisconnect:
            return workspace_error("invalid_workspace_request", request_id)
        try:
            if single:
                record = await run_in_threadpool(service.get, request.path_params["workspace_id"])
                body = public_workspace(record)
            else:
                try:
                    after = cursor_after(query["cursor"]) if "cursor" in query else None
                except ValueError:
                    return workspace_error("invalid_workspace_cursor", request_id)
                page = await run_in_threadpool(
                    service.list, WorkspaceQuery(int(query.get("limit", default_limit)), after)
                )
                body = dict(
                    parse(
                        "WorkspaceList",
                        {
                            "object": "nexus.list",
                            "data": [public_workspace(row) for row in page.items],
                            "has_more": page.next_id is not None,
                            "next_cursor": cursor_for(page.next_id)
                            if page.next_id is not None
                            else None,
                        },
                    )
                )
            validate_response(
                operation, 200, {"body": body, "headers": {"X-Request-ID": request_id}}
            )
        except WorkspaceNotFoundError:
            return workspace_error("workspace_not_found", request_id)
        except WorkspaceUnavailableError, ContractValidationError, ValueError:
            return workspace_error("workspace_service_unavailable", request_id)
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    @router.get(PREFIX, operation_id="listWorkspaces")
    async def list_workspaces(request: Request) -> Response:
        return await read(request, single=False)

    @router.get(PREFIX + "/{workspace_id}", operation_id="getWorkspace")
    async def get_workspace(request: Request) -> Response:
        return await read(request, single=True)

    return router
