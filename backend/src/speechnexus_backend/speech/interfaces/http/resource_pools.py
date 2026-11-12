"""Administrative pool read projection; canonical DTO validation stays at the edge.

The response is assembled from validated domain values, so an internal column
never reaches the wire: a pool row carries no command, credential, idempotency
key or storage identity the contract does not declare.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin import parse
from speechnexus_contracts.admin_http import validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.resource_pools import ResourcePoolService
from speechnexus_backend.speech.domain.resource_pools import (
    ResourcePool,
    ResourcePoolsUnavailableError,
)

PREFIX = "/v1/nexus/admin/resource-pools"
_ERRORS = {
    "invalid_resource_pool_request": (
        400,
        "invalid_request_error",
        "Invalid resource pool request.",
    ),
    "resource_pool_service_unavailable": (
        503,
        "service_unavailable_error",
        "Resource pool storage is unavailable.",
    ),
}


def resource_pool_error(code: str, request_id: str) -> JSONResponse:
    if code not in _ERRORS:
        code = "resource_pool_service_unavailable"
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


def public_pool(pool: ResourcePool) -> dict[str, Any]:
    """One contract-shaped row: every declared field and nothing else."""
    return dict(
        parse(
            "ResourcePool",
            {
                "id": pool.pool_id,
                "object": "nexus.resource_pool",
                "node_ids": list(pool.node_ids),
                "service_class": pool.service_class,
                "max_active_slots": pool.max_active_slots,
                "realtime_reserved_slots": pool.realtime_reserved_slots,
                "active_slots": pool.active_slots,
                "quarantine_slots": pool.quarantine_slots,
                "status": pool.status,
            },
        )
    )


def create_resource_pool_router(
    get_service: Callable[[], ResourcePoolService | None],
) -> APIRouter:
    router = APIRouter(tags=["resource-pools"])

    @router.get(PREFIX, operation_id="listResourcePools")
    async def list_resource_pools(request: Request) -> Response:
        request_id = str(request.state.request_id)
        service = get_service()
        if service is None:
            return resource_pool_error("resource_pool_service_unavailable", request_id)
        operation = "listResourcePools"
        if len(request.scope.get("query_string", b"")) > 4096:
            return resource_pool_error("invalid_resource_pool_request", request_id)
        pairs = list(request.query_params.multi_items())
        ids = request.headers.getlist("x-request-id")
        lengths = request.headers.getlist("content-length")
        try:
            # This listing declares no query parameter: accepting an ignored
            # filter would let a caller believe it paginated or filtered.
            if pairs or len(ids) > 1:
                raise ContractValidationError("A resource pool read has no query parameter")
            if (
                len(lengths) > 1
                or (lengths and lengths[0] != "0")
                or request.headers.getlist("transfer-encoding")
            ):
                raise ContractValidationError("A resource pool read has no body")
            # Empty ASGI frames may precede EOF. Bound both frames and time,
            # rejecting any bytes without allocating a JSON/body buffer.
            async with asyncio.timeout(1):
                for _ in range(8):
                    message = await request.receive()
                    if message["type"] != "http.request" or message.get("body"):
                        raise ContractValidationError("A resource pool read has no body")
                    if not message.get("more_body", False):
                        break
                else:
                    raise ContractValidationError("Resource pool receive budget exceeded")
            validate_request(
                operation,
                {
                    "path": {},
                    "query": {},
                    "headers": {"x-request-id": ids[0]} if ids else {},
                },
            )
        except ContractValidationError, TimeoutError, ClientDisconnect:
            return resource_pool_error("invalid_resource_pool_request", request_id)
        try:
            pools = await run_in_threadpool(service.list)
            body = dict(
                parse(
                    "ResourcePoolList",
                    {"object": "nexus.list", "data": [public_pool(pool) for pool in pools]},
                )
            )
            validate_response(
                operation, 200, {"body": body, "headers": {"X-Request-ID": request_id}}
            )
        except ResourcePoolsUnavailableError, ContractValidationError, ValueError:
            return resource_pool_error("resource_pool_service_unavailable", request_id)
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    return router
