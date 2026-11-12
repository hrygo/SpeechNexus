"""Administrative model binding listing; canonical DTO validation stays at the edge.

The response is assembled from validated domain values, so an internal column
never reaches the wire: the projection carries no command, credential,
idempotency key, digest, intent or storage identity the contract does not
declare. Keyset cursors freeze the selected status filter so a cursor cannot be
reused under a different filter and silently skip or repeat rows.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin import parse
from speechnexus_contracts.admin_http import contract, validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.binding_summaries import BindingSummaryService
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.binding_summaries import (
    BINDING_PUBLICATION_STATUSES,
    BindingPublicationStatus,
    BindingSummaryQuery,
    BindingSummaryUnavailableError,
    ModelBindingSummary,
)

PREFIX = "/v1/nexus/admin/model-bindings"
_RESOURCE = "model-binding-list"
_ERRORS = {
    "invalid_model_binding_request": (
        400,
        "invalid_request_error",
        "Invalid model binding request.",
    ),
    "invalid_model_binding_cursor": (
        400,
        "invalid_request_error",
        "Invalid model binding cursor.",
    ),
    "model_binding_service_unavailable": (
        503,
        "service_unavailable_error",
        "Model binding storage is unavailable.",
    ),
}


def model_binding_error(code: str, request_id: str) -> JSONResponse:
    if code not in _ERRORS:
        code = "model_binding_service_unavailable"
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


def cursor_for(after_id: str, status: BindingPublicationStatus | None) -> str:
    identifier(after_id)
    body = json.dumps(
        {"v": 1, "resource": _RESOURCE, "status": status, "after": after_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def cursor_after(value: str, status: BindingPublicationStatus | None) -> str:
    """Reject a cursor that is not exactly one this status filter issued."""
    try:
        if not 1 <= len(value) <= 512:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {"v", "resource", "status", "after"}:
            raise ValueError
        recorded = document["status"]
        if (
            type(document["v"]) is not int
            or document["v"] != 1
            or document["resource"] != _RESOURCE
        ):
            raise ValueError
        if recorded is not None and (
            not isinstance(recorded, str) or recorded not in BINDING_PUBLICATION_STATUSES
        ):
            raise ValueError
        if recorded != status:
            raise ValueError
        after = identifier(document["after"])
        if cursor_for(after, status) != value:
            raise ValueError
        return after
    except ValueError, TypeError, KeyError, binascii.Error, UnicodeError:
        raise ValueError("Invalid model binding cursor") from None


def public_binding_summary(summary: ModelBindingSummary) -> dict[str, Any]:
    """One contract-shaped row: every declared field and nothing else."""
    return dict(
        parse(
            "ModelBindingSummary",
            {
                "binding_id": summary.binding_id,
                "latest_revision": summary.latest_revision,
                "current_revision": summary.current_revision,
                "logical_model": summary.logical_model,
                "tasks": list(summary.tasks),
                "release_tier": summary.release_tier,
                "publication_status": summary.publication_status,
            },
        )
    )


def create_model_binding_list_router(
    get_service: Callable[[], BindingSummaryService | None],
) -> APIRouter:
    router = APIRouter(tags=["model-bindings"])
    # The declared default is read from the authoritative contract instead of
    # being copied into a second query DTO.
    default_limit = contract()["components"]["parameters"]["Limit"]["schema"]["default"]
    if type(default_limit) is not int:
        raise ValueError("Model binding list limit default must be an integer")

    @router.get(PREFIX, operation_id="listModelBindings")
    async def list_model_bindings(request: Request) -> Response:
        request_id = str(request.state.request_id)
        service = get_service()
        if service is None:
            return model_binding_error("model_binding_service_unavailable", request_id)
        operation = "listModelBindings"
        if len(request.scope.get("query_string", b"")) > 4096:
            return model_binding_error("invalid_model_binding_request", request_id)
        pairs = list(request.query_params.multi_items())
        ids = request.headers.getlist("x-request-id")
        lengths = request.headers.getlist("content-length")
        try:
            # This listing declares no body and no repeated parameter: accepting
            # either would make the caller's pagination or filter ambiguous.
            if len(pairs) != len({name for name, _ in pairs}) or len(ids) > 1:
                raise ContractValidationError("Duplicate model binding parameter")
            if (
                len(lengths) > 1
                or (lengths and lengths[0] != "0")
                or request.headers.getlist("transfer-encoding")
            ):
                raise ContractValidationError("A model binding read has no body")
            # Empty ASGI frames may precede EOF. Bound both frames and time,
            # rejecting any bytes without allocating a JSON/body buffer.
            async with asyncio.timeout(1):
                for _ in range(8):
                    message = await request.receive()
                    if message["type"] != "http.request" or message.get("body"):
                        raise ContractValidationError("A model binding read has no body")
                    if not message.get("more_body", False):
                        break
                else:
                    raise ContractValidationError("Model binding receive budget exceeded")
            query = dict(pairs)
            envelope = {
                "path": {},
                "query": query,
                "headers": {"x-request-id": ids[0]} if ids else {},
            }
            validate_request(operation, envelope)
        except ContractValidationError, TimeoutError, ClientDisconnect:
            return model_binding_error("invalid_model_binding_request", request_id)
        selected: BindingPublicationStatus | None = None
        status = query.get("status")
        if status is not None:
            if status not in BINDING_PUBLICATION_STATUSES:
                return model_binding_error("invalid_model_binding_request", request_id)
            selected = status
        try:
            after = cursor_after(query["cursor"], selected) if "cursor" in query else None
        except ValueError:
            return model_binding_error("invalid_model_binding_cursor", request_id)
        try:
            page = await run_in_threadpool(
                service.list,
                BindingSummaryQuery(int(query.get("limit", default_limit)), after, selected),
            )
            body = dict(
                parse(
                    "ModelBindingList",
                    {
                        "object": "nexus.list",
                        "data": [public_binding_summary(item) for item in page.items],
                        "has_more": page.next_id is not None,
                        "next_cursor": (
                            None if page.next_id is None else cursor_for(page.next_id, selected)
                        ),
                    },
                )
            )
            validate_response(
                operation, 200, {"body": body, "headers": {"X-Request-ID": request_id}}
            )
        except BindingSummaryUnavailableError, ContractValidationError, ValueError:
            return model_binding_error("model_binding_service_unavailable", request_id)
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    return router
