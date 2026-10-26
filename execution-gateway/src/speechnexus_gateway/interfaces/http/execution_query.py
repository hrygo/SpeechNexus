"""Read-only execution HTTP; no engine dispatch or reconciliation side effects."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import JSONResponse
from speechnexus_contracts.execution import parse
from starlette.concurrency import run_in_threadpool

from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.domain.fencing import IngressUnavailableError
from speechnexus_gateway.interfaces.http.control_errors import control_response
from speechnexus_gateway.interfaces.http.control_schemas import (
    ExecutionAudioStagingResultResponse,
    ExecutionStagingResultResponse,
    ExecutionStatus,
    wire_schema,
)
from speechnexus_gateway.interfaces.http.control_security import ComponentCredential


def _service(request: Request) -> ExecutionQueryService:
    service = request.app.state.execution_query
    if not isinstance(service, ExecutionQueryService):
        raise IngressUnavailableError("gateway query service is not running")
    return service


def create_execution_query_router(*, credential: ComponentCredential) -> APIRouter:
    router = APIRouter(tags=["execution-control"], dependencies=[Depends(credential)])
    headers = {"X-Request-ID": {"schema": {"type": "string"}}}
    errors: dict[int | str, dict[str, Any]] = {
        code: {
            "description": message,
            "headers": headers,
            "content": {
                "application/json": {"schema": wire_schema("ExecutionControlErrorResponse")}
            },
        }
        for code, message in {
            401: "Invalid component credential.",
            404: "No execution with this identity is recorded on this gateway.",
            422: "Invalid submit identity or correlation header.",
            503: "The original state cannot be read; follow the recovery instruction.",
        }.items()
    }

    @router.get(
        "/internal/v1/executions/{submit_intent_id}",
        operation_id="getExecution",
        summary=(
            "Query authoritative submit intent, engine execution state, "
            "output watermark, and cleanup evidence"
        ),
        response_model=ExecutionStatus,
        responses={200: {"headers": headers}, **errors},
    )
    async def get_execution(
        submit_intent_id: Annotated[
            str,
            Path(pattern=r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$", min_length=3, max_length=128),
        ],
        service: Annotated[ExecutionQueryService, Depends(_service)],
        _request_id: Annotated[
            str | None, Header(alias="X-Request-ID", min_length=1, max_length=128)
        ] = None,
    ) -> ExecutionStatus | JSONResponse:
        snapshot = await run_in_threadpool(service.query_outcome, submit_intent_id)
        if snapshot is None:
            return control_response(
                "control_execution_not_found", "No execution is recorded on this gateway.", 404
            )
        return parse("ExecutionStatusResponse", snapshot.status())

    @router.get(
        "/internal/v1/executions/{submit_intent_id}/staging-result",
        operation_id="getExecutionStagingResult",
        summary="Read the immutable private result version without publishing or executing",
        response_model=ExecutionStagingResultResponse,
        responses={200: {"headers": headers}, **errors},
    )
    async def get_staging_result(
        submit_intent_id: Annotated[
            str,
            Path(pattern=r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$", min_length=3, max_length=128),
        ],
        service: Annotated[ExecutionQueryService, Depends(_service)],
        _request_id: Annotated[
            str | None, Header(alias="X-Request-ID", min_length=1, max_length=128)
        ] = None,
    ) -> ExecutionStagingResultResponse | JSONResponse:
        snapshot = await run_in_threadpool(service.query_outcome, submit_intent_id)
        if snapshot is None:
            return control_response(
                "control_execution_not_found", "No execution is recorded on this gateway.", 404
            )
        return parse("ExecutionStagingResultResponse", snapshot.staging_response())

    @router.get(
        "/internal/v1/executions/{submit_intent_id}/audio-staging-result",
        operation_id="getExecutionAudioStagingResult",
        summary="Read a private audio receipt without publishing or executing",
        response_model=ExecutionAudioStagingResultResponse,
        responses={200: {"headers": headers}, **errors},
    )
    async def get_audio_staging_result(
        submit_intent_id: Annotated[
            str,
            Path(pattern=r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$", min_length=3, max_length=128),
        ],
        service: Annotated[ExecutionQueryService, Depends(_service)],
        _request_id: Annotated[
            str | None, Header(alias="X-Request-ID", min_length=1, max_length=128)
        ] = None,
    ) -> ExecutionAudioStagingResultResponse | JSONResponse:
        snapshot = await run_in_threadpool(service.query_outcome, submit_intent_id)
        if snapshot is None:
            return control_response(
                "control_execution_not_found", "No execution is recorded on this gateway.", 404
            )
        return parse("ExecutionAudioStagingResultResponse", snapshot.audio_staging_response())

    return router
