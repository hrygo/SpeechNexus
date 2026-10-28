"""Authenticated submit and abort routes.

Both routes refuse with a stable recovery instruction instead of inventing engine facts:
without a configured engine adapter nothing is recorded, and an abort that the adapter
cannot verify never reports a stop.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import JSONResponse
from speechnexus_contracts.execution import parse
from starlette.concurrency import run_in_threadpool

from speechnexus_gateway.application.abort_execution import (
    AbortExecutionRequest,
    ExecutionAbortService,
)
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.recovery import submission_response
from speechnexus_gateway.interfaces.execution_payloads import (
    derived_stream_id_for,
    submit_request_from_payload,
)
from speechnexus_gateway.interfaces.http.control_errors import control_response
from speechnexus_gateway.interfaces.http.control_schemas import (
    AbortExecutionPayload,
    ExecutionStatus,
    SubmitExecutionPayload,
    SubmitExecutionResponse,
    wire_schema,
)
from speechnexus_gateway.interfaces.http.control_security import ComponentCredential

_REQUEST_ID_HEADER = {"X-Request-ID": {"schema": {"type": "string"}}}
_SUBMIT_PATH = "/internal/v1/executions/submit"
_ABORT_PATH = "/internal/v1/executions/{submit_intent_id}/abort"
_ABORT_INTENT_PATH = "/internal/v1/executions/{submit_intent_id}/abort-intents"
_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$"
_NO_DISPATCH_ADAPTER = (
    "No engine dispatch adapter is configured on this gateway; the submission was not recorded."
)
_NO_ABORT_ADAPTER = (
    "No engine abort adapter is configured on this gateway; the execution state is unchanged."
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


def create_execution_submit_router(*, credential: ComponentCredential) -> APIRouter:
    router = APIRouter(tags=["execution-control"], dependencies=[Depends(credential)])
    submit_errors = _error_responses(
        {
            401: "Missing or invalid gateway component credential.",
            409: (
                "Stale fencing token, fenced ingress, instance generation mismatch, "
                "or a conflicting submit intent."
            ),
            422: "The execution request failed schema, canonical or lease validation.",
            503: "No sealed acknowledgment; follow the explicit recovery instruction.",
        }
    )
    abort_errors = _error_responses(
        {
            401: "Missing or invalid gateway component credential.",
            404: "No execution with this identity is recorded on this gateway.",
            409: "Stale fencing token or an execution state that cannot be aborted.",
            422: "The abort request failed schema or identifier validation.",
            503: "No acknowledgment; follow the explicit recovery instruction.",
        }
    )

    @router.post(
        _SUBMIT_PATH,
        operation_id="submitExecution",
        summary=(
            "Validate lease/fencing and persist a unique submit intent before engine dispatch"
        ),
        response_model=SubmitExecutionResponse,
        responses={200: {"headers": _REQUEST_ID_HEADER}, **submit_errors},
    )
    async def submit_execution(
        payload: SubmitExecutionPayload,
        request: Request,
        _request_id: Annotated[
            str | None, Header(alias="X-Request-ID", min_length=1, max_length=128)
        ] = None,
    ) -> SubmitExecutionResponse | JSONResponse:
        service = request.app.state.submit_execution
        if not isinstance(service, SubmitExecutionService):
            return control_response(
                "control_store_unavailable", _NO_DISPATCH_ADAPTER, 503, "retry_transaction"
            )
        outcome = await run_in_threadpool(
            service.submit,
            submit_request_from_payload(payload, output_stream_id=derived_stream_id_for(payload)),
        )
        return parse(
            "SubmitExecutionResponse",
            submission_response(outcome.intent, idempotent_replay=outcome.idempotent_replay),
        )

    @router.post(
        _ABORT_PATH,
        operation_id="abortExecution",
        summary="Request engine abort and return verified stop or quarantine cleanup evidence",
        response_model=ExecutionStatus,
        responses={200: {"headers": _REQUEST_ID_HEADER}, **abort_errors},
    )
    async def abort_execution(
        submit_intent_id: Annotated[
            str,
            Path(
                pattern=_IDENTIFIER_PATTERN,
                min_length=3,
                max_length=128,
            ),
        ],
        payload: AbortExecutionPayload,
        request: Request,
        _request_id: Annotated[
            str | None, Header(alias="X-Request-ID", min_length=1, max_length=128)
        ] = None,
    ) -> ExecutionStatus | JSONResponse:
        service = request.app.state.abort_execution
        if not isinstance(service, ExecutionAbortService):
            return control_response(
                "control_store_unavailable", _NO_ABORT_ADAPTER, 503, "retry_transaction"
            )
        snapshot = await run_in_threadpool(
            service.abort,
            AbortExecutionRequest(
                submit_intent_id=submit_intent_id,
                operation_id=payload["operation_id"],
                attempt_id=payload["attempt_id"],
                fencing_token=payload["fencing_token"],
                reason=payload["reason"],
                wait_timeout_ms=payload["wait_timeout_ms"],
            ),
        )
        if snapshot is None:
            return control_response(
                "control_execution_not_found", "No execution is recorded on this gateway.", 404
            )
        return parse("ExecutionStatusResponse", snapshot.status())

    @router.post(
        _ABORT_INTENT_PATH,
        operation_id="acceptAbortIntent",
        summary="Persist and recover an original abort request",
        response_model=ExecutionStatus,
        responses={200: {"headers": _REQUEST_ID_HEADER}, **abort_errors},
    )
    async def accept_abort_intent(
        submit_intent_id: Annotated[
            str, Path(pattern=_IDENTIFIER_PATTERN, min_length=3, max_length=128)
        ],
        payload: AbortExecutionPayload,
        request: Request,
        _request_id: Annotated[
            str | None, Header(alias="X-Request-ID", min_length=1, max_length=128)
        ] = None,
    ) -> ExecutionStatus | JSONResponse:
        service = request.app.state.abort_execution
        if not isinstance(service, ExecutionAbortService):
            return control_response(
                "control_store_unavailable", _NO_ABORT_ADAPTER, 503, "retry_transaction"
            )
        snapshot = await run_in_threadpool(
            service.abort_idempotent,
            AbortExecutionRequest(
                submit_intent_id=submit_intent_id,
                operation_id=payload["operation_id"],
                attempt_id=payload["attempt_id"],
                fencing_token=payload["fencing_token"],
                reason=payload["reason"],
                wait_timeout_ms=payload["wait_timeout_ms"],
            ),
        )
        if snapshot is None:
            return control_response(
                "control_execution_not_found", "No execution is recorded on this gateway.", 404
            )
        return parse("ExecutionStatusResponse", snapshot.status())

    return router
