"""Real shared-group reads and the two accepted writes at the HTTP boundary.

The write bodies are read with an explicit byte and time budget before any DTO
validation, so an oversized or slow body never reaches the authority. Both
operations require canonical request validation; the declared responses are
validated before they leave this boundary, including the error statuses.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
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
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspacePolicyUnsupportedError,
    WorkspaceQuery,
    WorkspaceUnavailableError,
)
from speechnexus_backend.speech.interfaces.http.origin_policy import rejected_authority

_LOG = logging.getLogger(__name__)
PREFIX = "/v1/nexus/workspaces"
CREATE, UPDATE = "createWorkspace", "updateWorkspace"
# The declared bodies carry one display name, two enum words and one integer.
# The bound is generous for that shape and still refuses an unbounded body.
MAX_WRITE_BODY_BYTES = 4096
WRITE_RECEIVE_TIMEOUT_SECONDS = 2
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
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "The key identifies another workspace intent.",
    ),
    "configuration_changed": (
        409,
        "invalid_request_error",
        "The workspace metadata revision changed.",
    ),
    "workspace_identity_conflict": (
        409,
        "invalid_request_error",
        "The workspace identity is already taken.",
    ),
    "workspace_policy_unsupported": (
        422,
        "invalid_request_error",
        "This deployment has no policy registry for policy_id.",
    ),
    "workspace_service_unavailable": (
        503,
        "service_unavailable_error",
        "Workspace storage is unavailable.",
    ),
}


def workspace_error(
    code: str,
    request_id: str,
    *,
    operation: str | None = None,
    param: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Emit one fixed public error; a write error is checked against its operation."""
    if code not in _ERRORS:
        code = "workspace_service_unavailable"
    status, kind, message = _ERRORS[code]
    response_headers = {
        "Cache-Control": "no-store",
        "X-Should-Retry": "false",
        **(headers or {}),
    }
    body = dict(
        parse(
            "ErrorResponse",
            {
                "error": {
                    "code": code,
                    "type": kind,
                    "message": message,
                    "param": param,
                    "request_id": request_id,
                    "retryable": False,
                }
            },
        )
    )
    if operation is not None:
        try:
            validate_response(operation, status, {"body": body, "headers": response_headers})
        except ContractValidationError:
            # A status the frozen contract does not declare is a defect in the
            # boundary, not a reason to rewrite one error into another. Keep the
            # stable mapping and record the drift loudly.
            _LOG.warning("Workspace %s error status %s is not declared", operation, status)
    return JSONResponse(status_code=status, headers=response_headers, content=body)


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


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def _json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)


async def _write_envelope(request: Request) -> dict[str, Any]:
    """Read one bounded canonical write envelope; never drain an oversized body."""
    if request.scope.get("query_string"):
        raise ContractValidationError("Workspace writes declare no query")
    headers: dict[str, str] = {}
    for name in ("x-request-id", "idempotency-key"):
        values = request.headers.getlist(name)
        if len(values) > 1:
            raise ContractValidationError("Ambiguous header")
        if values:
            headers[name] = values[0]
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise ContractValidationError("Workspace writes accept application/json only")
    body = bytearray()
    async with asyncio.timeout(WRITE_RECEIVE_TIMEOUT_SECONDS):
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_WRITE_BODY_BYTES:
                raise ContractValidationError("Workspace write body exceeds its bound")
            body.extend(chunk)
    return {
        "path": request.path_params,
        "headers": headers,
        "body": _json(bytes(body)),
    }


def _created(service: WorkspaceService, envelope: dict[str, Any], request_id: str) -> Response:
    try:
        receipt = service.create(
            display_name=envelope["body"]["display_name"],
            policy_id=envelope["body"].get("policy_id"),
            request_id=request_id,
            idempotency_key=envelope["headers"].get("idempotency-key"),
        )
        wire = public_workspace(receipt.workspace)
        validate_response(CREATE, 201, {"body": wire, "headers": {"X-Request-ID": request_id}})
        return JSONResponse(wire, status_code=201, headers={"Cache-Control": "no-store"})
    except WorkspacePolicyUnsupportedError:
        return workspace_error(
            "workspace_policy_unsupported", request_id, operation=CREATE, param="policy_id"
        )
    except WorkspaceConflictError as error:
        return workspace_error(error.code, request_id, operation=CREATE)
    except WorkspaceNotFoundError:
        return workspace_error("workspace_not_found", request_id, operation=CREATE)
    except ValueError:
        # Defensive: canonical validation already refuses these shapes.
        return workspace_error("invalid_workspace_request", request_id, operation=CREATE)
    except Exception:
        # No driver, validation or caller data leaves this boundary.
        return workspace_error("workspace_service_unavailable", request_id, operation=CREATE)


def _updated(service: WorkspaceService, envelope: dict[str, Any], request_id: str) -> Response:
    body = envelope["body"]
    try:
        receipt = service.update(
            workspace_id=envelope["path"]["workspace_id"],
            # JSON Schema integer also admits an integral JSON number.
            expected_metadata_revision=int(body["expected_metadata_revision"]),
            display_name=body.get("display_name"),
            status=body.get("status"),
            request_id=request_id,
            idempotency_key=envelope["headers"].get("idempotency-key"),
        )
        wire = public_workspace(receipt.workspace)
        validate_response(UPDATE, 200, {"body": wire, "headers": {"X-Request-ID": request_id}})
        return JSONResponse(wire, headers={"Cache-Control": "no-store"})
    except WorkspaceConflictError as error:
        return workspace_error(error.code, request_id, operation=UPDATE)
    except WorkspaceNotFoundError:
        return workspace_error("workspace_not_found", request_id, operation=UPDATE)
    except ValueError:
        return workspace_error("invalid_workspace_request", request_id, operation=UPDATE)
    except Exception:
        return workspace_error("workspace_service_unavailable", request_id, operation=UPDATE)


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

    async def write(request: Request, *, operation: str) -> Response:
        request_id = str(request.state.request_id)
        service = get_service()
        if service is None:
            return workspace_error("workspace_service_unavailable", request_id, operation=operation)
        try:
            envelope = await _write_envelope(request)
            validate_request(operation, envelope)
        except ContractValidationError, TimeoutError, ClientDisconnect, ValueError, TypeError:
            return workspace_error("invalid_workspace_request", request_id, operation=operation)
        handler = _created if operation == CREATE else _updated
        return await run_in_threadpool(handler, service, envelope, request_id)

    @router.get(PREFIX, operation_id="listWorkspaces")
    async def list_workspaces(request: Request) -> Response:
        return await read(request, single=False)

    @router.post(PREFIX, operation_id=CREATE)
    async def create_workspace(request: Request) -> Response:
        return await write(request, operation=CREATE)

    @router.get(PREFIX + "/{workspace_id}", operation_id="getWorkspace")
    async def get_workspace(request: Request) -> Response:
        return await read(request, single=True)

    @router.patch(PREFIX + "/{workspace_id}", operation_id=UPDATE)
    async def update_workspace(request: Request) -> Response:
        return await write(request, operation=UPDATE)

    return router
