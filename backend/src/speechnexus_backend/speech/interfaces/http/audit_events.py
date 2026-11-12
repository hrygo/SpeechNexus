"""Administrative audit event listing; canonical DTO validation stays at the edge.

The response is assembled from validated domain values, so an internal column
never reaches the wire: the projection carries no command, idempotency key,
intent digest, evidence identity or storage path the contract does not declare.
The keyset cursor freezes both the composite position and the selected action
filter, so a cursor cannot be reused under another filter and silently skip or
repeat a row.
"""

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
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.audit_events import AuditEventService
from speechnexus_backend.speech.domain.audit_events import (
    AuditEvent,
    AuditEventPosition,
    AuditEventQuery,
    AuditEventUnavailableError,
)

PREFIX = "/v1/nexus/audit-events"
_RESOURCE = "audit-event-list"
_ERRORS = {
    "invalid_audit_request": (
        400,
        "invalid_request_error",
        "Invalid audit event request.",
    ),
    "invalid_audit_cursor": (
        400,
        "invalid_request_error",
        "Invalid audit event cursor.",
    ),
    "audit_service_unavailable": (
        503,
        "service_unavailable_error",
        "Audit event storage is unavailable.",
    ),
}


def audit_error(code: str, request_id: str) -> JSONResponse:
    if code not in _ERRORS:
        code = "audit_service_unavailable"
    status, kind, message = _ERRORS[code]
    return JSONResponse(
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Should-Retry": "false"},
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


def cursor_for(position: AuditEventPosition, action: str | None) -> str:
    body = json.dumps(
        {
            "v": 1,
            "resource": _RESOURCE,
            "action": action,
            "occurred_at_us": position.occurred_at_us,
            "event_id": position.event_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def cursor_position(value: str, action: str | None) -> AuditEventPosition:
    """Reject a cursor that is not exactly one this action filter issued."""
    try:
        if not 1 <= len(value) <= 512:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {
            "v",
            "resource",
            "action",
            "occurred_at_us",
            "event_id",
        }:
            raise ValueError
        if (
            type(document["v"]) is not int
            or document["v"] != 1
            or document["resource"] != _RESOURCE
        ):
            raise ValueError
        recorded = document["action"]
        if recorded is not None and not isinstance(recorded, str):
            raise ValueError
        if recorded != action:
            raise ValueError
        occurred_at_us = document["occurred_at_us"]
        if isinstance(occurred_at_us, bool) or not isinstance(occurred_at_us, int):
            raise ValueError
        position = AuditEventPosition(occurred_at_us, document["event_id"])
        if cursor_for(position, action) != value:
            raise ValueError
        return position
    except ValueError, TypeError, KeyError, binascii.Error, UnicodeError:
        raise ValueError("Invalid audit event cursor") from None


def occurred_at(occurred_at_us: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=occurred_at_us)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def public_event(event: AuditEvent) -> dict[str, Any]:
    """One contract-shaped row: every declared field and nothing else."""
    summary = event.change_summary
    return dict(
        parse(
            "AuditEvent",
            {
                "id": event.event_id,
                "object": "nexus.audit_event",
                "actor_type": event.actor_type,
                "credential_label": event.credential_label,
                "request_id": event.request_id,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "occurred_at": occurred_at(event.occurred_at_us),
                "policy_revision": str(event.policy_revision),
                "change_summary": (
                    None
                    if summary is None
                    else {
                        "changed_fields": list(summary.changed_fields),
                        "reason": summary.reason,
                    }
                ),
            },
        )
    )


def create_audit_event_router(
    get_service: Callable[[], AuditEventService | None],
) -> APIRouter:
    router = APIRouter(tags=["audit"])
    # The declared default is read from the authoritative contract instead of
    # being copied into a second query DTO.
    default_limit = contract()["components"]["parameters"]["Limit"]["schema"]["default"]
    if type(default_limit) is not int:
        raise ValueError("Audit event list limit default must be an integer")

    @router.get(PREFIX, operation_id="listAuditEvents")
    async def list_audit_events(request: Request) -> Response:
        request_id = str(request.state.request_id)
        service = get_service()
        if service is None:
            return audit_error("audit_service_unavailable", request_id)
        operation = "listAuditEvents"
        if len(request.scope.get("query_string", b"")) > 4096:
            return audit_error("invalid_audit_request", request_id)
        pairs = list(request.query_params.multi_items())
        ids = request.headers.getlist("x-request-id")
        lengths = request.headers.getlist("content-length")
        try:
            # This listing declares no body and no repeated parameter: accepting
            # either would make the caller's pagination or filter ambiguous.
            if len(pairs) != len({name for name, _ in pairs}) or len(ids) > 1:
                raise ContractValidationError("Duplicate audit event parameter")
            if (
                len(lengths) > 1
                or (lengths and lengths[0] != "0")
                or request.headers.getlist("transfer-encoding")
            ):
                raise ContractValidationError("An audit event read has no body")
            # Empty ASGI frames may precede EOF. Bound both frames and time,
            # rejecting any bytes without allocating a JSON/body buffer.
            async with asyncio.timeout(1):
                for _ in range(8):
                    message = await request.receive()
                    if message["type"] != "http.request" or message.get("body"):
                        raise ContractValidationError("An audit event read has no body")
                    if not message.get("more_body", False):
                        break
                else:
                    raise ContractValidationError("Audit event receive budget exceeded")
            query = dict(pairs)
            envelope = {
                "path": {},
                "query": query,
                "headers": {"x-request-id": ids[0]} if ids else {},
            }
            validate_request(operation, envelope)
        except ContractValidationError, TimeoutError, ClientDisconnect:
            return audit_error("invalid_audit_request", request_id)
        action = query.get("action")
        try:
            after = cursor_position(query["cursor"], action) if "cursor" in query else None
        except ValueError:
            return audit_error("invalid_audit_cursor", request_id)
        try:
            page = await run_in_threadpool(
                service.list,
                AuditEventQuery(int(query.get("limit", default_limit)), after, action),
            )
            body = dict(
                parse(
                    "AuditEventList",
                    {
                        "object": "nexus.list",
                        "data": [public_event(item) for item in page.items],
                        "has_more": page.next_position is not None,
                        "next_cursor": (
                            None
                            if page.next_position is None
                            else cursor_for(page.next_position, action)
                        ),
                    },
                )
            )
            validate_response(
                operation, 200, {"body": body, "headers": {"X-Request-ID": request_id}}
            )
        except AuditEventUnavailableError, ContractValidationError, ValueError:
            return audit_error("audit_service_unavailable", request_id)
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    return router
