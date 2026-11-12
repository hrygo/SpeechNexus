"""Administrative reads of recorded runtime instance generations; DTO stays at the edge.

The response is assembled from validated domain values, so an internal column
never reaches the wire: no command, lease, submit intent or stop evidence
document is projected, and no fact the instance store does not record is
invented. The keyset cursor freezes both the composite position and the selected
filters, so a cursor cannot be reused under another filter and silently skip or
repeat a row.

Two misses are deliberately distinguishable. A 404 whose ``code`` is
``model_instance_unknown`` means the instance id has no recorded generation at
all; ``model_instance_generation_unknown`` means the id exists but this
generation does not. A consumer retrying a stale generation must not read that
as "this instance never existed".
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

from speechnexus_backend.speech.application.model_instances import ModelInstanceService
from speechnexus_backend.speech.domain.model_instances import (
    INSTANCE_STATES,
    InstancePosition,
    ModelInstance,
    ModelInstanceLookup,
    ModelInstanceQuery,
    ModelInstanceUnavailableError,
)

PREFIX = "/v1/nexus/admin/model-instances"
_RESOURCE = "model-instance-list"
_ERRORS = {
    "invalid_model_instance_request": (
        400,
        "invalid_request_error",
        "Invalid model instance request.",
    ),
    "invalid_model_instance_cursor": (
        400,
        "invalid_request_error",
        "Invalid model instance cursor.",
    ),
    "model_instance_unknown": (
        404,
        "not_found_error",
        "No generation of this instance is recorded.",
    ),
    "model_instance_generation_unknown": (
        404,
        "not_found_error",
        "This instance generation is not recorded.",
    ),
    "model_instance_service_unavailable": (
        503,
        "service_unavailable_error",
        "Model instance storage is unavailable.",
    ),
}


def model_instance_error(code: str, request_id: str) -> JSONResponse:
    if code not in _ERRORS:
        code = "model_instance_service_unavailable"
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


def cursor_for(position: InstancePosition, host_node_id: str | None, state: str | None) -> str:
    body = json.dumps(
        {
            "v": 1,
            "resource": _RESOURCE,
            "host_node_id": host_node_id,
            "state": state,
            "instance_id": position.instance_id,
            "generation": position.generation,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def cursor_position(value: str, host_node_id: str | None, state: str | None) -> InstancePosition:
    """Reject a cursor that is not exactly one this filter pair issued."""
    try:
        if not 1 <= len(value) <= 512:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {
            "v",
            "resource",
            "host_node_id",
            "state",
            "instance_id",
            "generation",
        }:
            raise ValueError
        if (
            type(document["v"]) is not int
            or document["v"] != 1
            or document["resource"] != _RESOURCE
            or document["host_node_id"] != host_node_id
            or document["state"] != state
        ):
            raise ValueError
        generation = document["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError
        position = InstancePosition(document["instance_id"], generation)
        if cursor_for(position, host_node_id, state) != value:
            raise ValueError
        return position
    except ValueError, TypeError, KeyError, binascii.Error, UnicodeError:
        raise ValueError("Invalid model instance cursor") from None


def recorded_at(timestamp_us: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=timestamp_us)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def public_instance(instance: ModelInstance) -> dict[str, Any]:
    """One contract-shaped row: every declared field and nothing else.

    ``readiness`` carries only ``accepting_new_work`` and the recorded readiness
    time, and ``observed`` carries only the recorded stop time. The store does
    not record loaded artifact digests, a process start time, a heartbeat time or
    an instance stop evidence document, so none of them can appear here.
    """
    return dict(
        parse(
            "ModelRuntimeInstance",
            {
                "schema_version": "1.0.0",
                "instance_id": instance.instance_id,
                "host_node_id": instance.host_node_id,
                "gateway_id": instance.gateway_id,
                "binding": {
                    "id": instance.binding_id,
                    "revision": instance.binding_revision,
                },
                "binding_revision_id": instance.binding_revision_id,
                "pool_id": instance.pool_id,
                "generation": instance.generation,
                "config_digest": instance.config_digest,
                "state": instance.state,
                "readiness": {
                    "accepting_new_work": instance.accepting_new_work,
                    "ready_at": (
                        None if instance.ready_at_us is None else recorded_at(instance.ready_at_us)
                    ),
                },
                "observed": {
                    "stopped_at": (
                        None
                        if instance.stopped_at_us is None
                        else recorded_at(instance.stopped_at_us)
                    )
                },
                "quarantine_reason": instance.current_quarantine_reason,
            },
        )
    )


def _generation(value: str) -> int:
    """Parse the declared integer path parameter after canonical validation."""
    if not 1 <= len(value) <= 17 or not value.isascii() or not value.isdigit():
        raise ValueError("Invalid instance generation")
    generation = int(value)
    if generation < 1 or value != str(generation):
        raise ValueError("Invalid instance generation")
    return generation


async def _framed_request(request: Request) -> list[str]:
    """Enforce the declared request framing and return the request ids."""
    pairs = list(request.query_params.multi_items())
    ids = request.headers.getlist("x-request-id")
    lengths = request.headers.getlist("content-length")
    if len(pairs) != len({name for name, _ in pairs}) or len(ids) > 1:
        raise ContractValidationError("Duplicate model instance parameter")
    if (
        len(lengths) > 1
        or (lengths and lengths[0] != "0")
        or request.headers.getlist("transfer-encoding")
    ):
        raise ContractValidationError("A model instance read has no body")
    # Empty ASGI frames may precede EOF. Bound both frames and time, rejecting
    # any bytes without allocating a JSON/body buffer.
    async with asyncio.timeout(1):
        for _ in range(8):
            message = await request.receive()
            if message["type"] != "http.request" or message.get("body"):
                raise ContractValidationError("A model instance read has no body")
            if not message.get("more_body", False):
                break
        else:
            raise ContractValidationError("Model instance receive budget exceeded")
    return ids


def create_model_instance_router(
    get_service: Callable[[], ModelInstanceService | None],
) -> APIRouter:
    router = APIRouter(tags=["model-instances"])
    # The declared default is read from the authoritative contract instead of
    # being copied into a second query DTO.
    default_limit = contract()["components"]["parameters"]["Limit"]["schema"]["default"]
    if type(default_limit) is not int:
        raise ValueError("Model instance list limit default must be an integer")

    @router.get(PREFIX, operation_id="listModelInstances")
    async def list_model_instances(request: Request) -> Response:
        request_id = str(request.state.request_id)
        service = get_service()
        if service is None:
            return model_instance_error("model_instance_service_unavailable", request_id)
        operation = "listModelInstances"
        if len(request.scope.get("query_string", b"")) > 4096:
            return model_instance_error("invalid_model_instance_request", request_id)
        pairs = list(request.query_params.multi_items())
        try:
            ids = await _framed_request(request)
            query = dict(pairs)
            envelope = {
                "path": {},
                "query": query,
                "headers": {"x-request-id": ids[0]} if ids else {},
            }
            validate_request(operation, envelope)
        except ContractValidationError, TimeoutError, ClientDisconnect:
            return model_instance_error("invalid_model_instance_request", request_id)
        host_node_id = query.get("host_node_id")
        state = query.get("state")
        if state is not None and state not in INSTANCE_STATES:
            return model_instance_error("invalid_model_instance_request", request_id)
        try:
            after = (
                cursor_position(query["cursor"], host_node_id, state) if "cursor" in query else None
            )
        except ValueError:
            return model_instance_error("invalid_model_instance_cursor", request_id)
        try:
            page = await run_in_threadpool(
                service.list,
                ModelInstanceQuery(
                    int(query.get("limit", default_limit)), after, host_node_id, state
                ),
            )
            body = dict(
                parse(
                    "ModelInstanceList",
                    {
                        "object": "nexus.list",
                        "data": [public_instance(item) for item in page.items],
                        "has_more": page.next_position is not None,
                        "next_cursor": (
                            None
                            if page.next_position is None
                            else cursor_for(page.next_position, host_node_id, state)
                        ),
                    },
                )
            )
            validate_response(
                operation, 200, {"body": body, "headers": {"X-Request-ID": request_id}}
            )
        except ModelInstanceUnavailableError, ContractValidationError, ValueError:
            return model_instance_error("model_instance_service_unavailable", request_id)
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    @router.get(
        PREFIX + "/{instance_id}/generations/{generation}",
        operation_id="getModelInstance",
    )
    async def get_model_instance(request: Request) -> Response:
        request_id = str(request.state.request_id)
        service = get_service()
        if service is None:
            return model_instance_error("model_instance_service_unavailable", request_id)
        operation = "getModelInstance"
        if len(request.scope.get("query_string", b"")) > 4096:
            return model_instance_error("invalid_model_instance_request", request_id)
        pairs = list(request.query_params.multi_items())
        try:
            ids = await _framed_request(request)
            path = {
                "instance_id": request.path_params["instance_id"],
                "generation": request.path_params["generation"],
            }
            envelope = {
                "path": path,
                "query": dict(pairs),
                "headers": {"x-request-id": ids[0]} if ids else {},
            }
            validate_request(operation, envelope)
            generation = _generation(path["generation"])
        except ContractValidationError, TimeoutError, ClientDisconnect, ValueError, KeyError:
            return model_instance_error("invalid_model_instance_request", request_id)
        try:
            lookup: ModelInstanceLookup = await run_in_threadpool(
                service.get, path["instance_id"], generation
            )
            if lookup.instance is None:
                code = (
                    "model_instance_generation_unknown"
                    if lookup.instance_exists
                    else "model_instance_unknown"
                )
                return model_instance_error(code, request_id)
            body = public_instance(lookup.instance)
            validate_response(
                operation, 200, {"body": body, "headers": {"X-Request-ID": request_id}}
            )
        except ModelInstanceUnavailableError, ContractValidationError, ValueError:
            return model_instance_error("model_instance_service_unavailable", request_id)
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    return router
