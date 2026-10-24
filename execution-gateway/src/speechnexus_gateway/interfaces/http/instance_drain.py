"""Authenticated instance drain: report unfinished work instead of asserting a stop.

``drained`` and ``safe_to_stop_instance`` are safety assertions, so this route only reports
them from its own fresh read of this gateway's unfinished intents for that instance
generation. Without an engine abort adapter the route still answers with the inventory and
never claims that the instance may be stopped.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import JSONResponse
from speechnexus_contracts.execution import parse
from starlette.concurrency import run_in_threadpool

from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.domain.instance_drain import DrainRequest
from speechnexus_gateway.interfaces.http.control_errors import control_response
from speechnexus_gateway.interfaces.http.control_schemas import (
    DrainInstancePayload,
    DrainInstanceResponse,
    wire_schema,
)
from speechnexus_gateway.interfaces.http.control_security import ComponentCredential

_REQUEST_ID_HEADER = {"X-Request-ID": {"schema": {"type": "string"}}}
_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$"
_NO_ADAPTER = (
    "No engine abort adapter is configured on this gateway; this drain only reports the "
    "recorded in-flight work and claims no stop."
)


def _error_responses(descriptions: dict[int, str]) -> dict[int | str, dict[str, Any]]:
    return {
        code: {
            "description": description,
            "headers": _REQUEST_ID_HEADER,
            "content": {
                "application/json": {"schema": wire_schema("ExecutionControlErrorResponse")}
            },
        }
        for code, description in descriptions.items()
    }


def create_instance_drain_router(*, credential: ComponentCredential) -> APIRouter:
    router = APIRouter(tags=["execution-control"], dependencies=[Depends(credential)])
    drain_errors = _error_responses(
        {
            401: "Missing, invalid, or revoked component credential.",
            409: "Stale fencing token or instance generation mismatch.",
            422: "Drain request failed schema validation.",
            503: "No acknowledgment; follow the explicit recovery instruction.",
        }
    )

    @router.post(
        "/internal/v1/instances/{instance_id}/drain",
        operation_id="drainEngineInstance",
        summary=(
            "Stop new dispatches to a specific engine instance generation "
            "and reconcile in-flight intents"
        ),
        response_model=DrainInstanceResponse,
        responses={200: {"headers": _REQUEST_ID_HEADER}, **drain_errors},
    )
    async def drain_engine_instance(
        instance_id: Annotated[
            str,
            Path(pattern=_IDENTIFIER_PATTERN, min_length=3, max_length=128),
        ],
        payload: DrainInstancePayload,
        request: Request,
        _request_id: Annotated[
            str | None, Header(alias="X-Request-ID", min_length=1, max_length=128)
        ] = None,
    ) -> DrainInstanceResponse | JSONResponse:
        service = request.app.state.instance_drain
        if not isinstance(service, InstanceDrainService):
            return control_response(
                "control_store_unavailable", _NO_ADAPTER, 503, "retry_transaction"
            )
        snapshot = await run_in_threadpool(
            service.drain,
            DrainRequest(
                instance_id=instance_id,
                host_node_id=payload["host_node_id"],
                pool_id=payload["pool_id"],
                instance_generation=payload["instance_generation"],
                fencing_token=payload["fencing_token"],
                max_drain_wait_ms=payload["max_drain_wait_ms"],
            ),
        )
        return parse("DrainInstanceResponse", snapshot.response())

    return router
