"""One implemented execution-control operation; engine routes remain unavailable."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Request
from speechnexus_contracts.execution import parse
from starlette.concurrency import run_in_threadpool

from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.domain.fencing import BarrierRequest, IngressUnavailableError
from speechnexus_gateway.interfaces.http.control_schemas import (
    GatewayBarrierPayload,
    GatewayBarrierResponse,
    wire_schema,
)
from speechnexus_gateway.interfaces.http.control_security import ComponentCredential

_REQUEST_ID_HEADER = {"X-Request-ID": {"schema": {"type": "string"}}}


def _service(request: Request) -> GatewayBarrierService:
    service = request.app.state.barrier_control
    if not isinstance(service, GatewayBarrierService):
        raise IngressUnavailableError("gateway control has not started or is shutting down")
    return service


def create_barrier_router(*, credential: ComponentCredential) -> APIRouter:
    router = APIRouter(tags=["execution-control"], dependencies=[Depends(credential)])
    errors: dict[int | str, dict[str, Any]] = {
        code: {
            "description": description,
            "headers": _REQUEST_ID_HEADER,
            "content": {
                "application/json": {"schema": wire_schema("ExecutionControlErrorResponse")}
            },
        }
        for code, description in {
            401: "Missing or invalid gateway component credential.",
            409: "Stale authority, conflicting proof or pending abort cleanup.",
            422: "Invalid barrier request or gateway identity.",
            503: "No acknowledgment; follow the explicit recovery instruction.",
        }.items()
    }

    @router.post(
        "/internal/v1/gateways/{gateway_id}/fencing-barrier",
        operation_id="enforceGatewayFencingBarrier",
        summary="Seal a gateway pool ingress against stale fencing tokens prior to owner takeover",
        responses={200: {"headers": _REQUEST_ID_HEADER}, **errors},
    )
    async def seal_gateway_barrier(
        gateway_id: Annotated[
            str,
            Path(pattern=r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$", min_length=3, max_length=128),
        ],
        payload: GatewayBarrierPayload,
        service: Annotated[GatewayBarrierService, Depends(_service)],
        _request_id: Annotated[
            str | None, Header(alias="X-Request-ID", min_length=1, max_length=128)
        ] = None,
    ) -> GatewayBarrierResponse:
        request = BarrierRequest(
            pool_id=payload["pool_id"],
            new_owner_scheduler_id=payload["new_owner_scheduler_id"],
            min_accepted_fencing_token=payload["min_accepted_fencing_token"],
            abort_in_flight=payload["abort_in_flight"],
        )
        snapshot = await run_in_threadpool(service.seal, gateway_id, request)
        return parse("GatewayFencingBarrierResponse", snapshot.response())

    return router
