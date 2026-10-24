"""Authenticated submit and abort routes.

Both routes refuse with a stable recovery instruction instead of inventing engine facts:
without a configured engine adapter nothing is recorded, and an abort that the adapter
cannot verify never reports a stop.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import JSONResponse
from speechnexus_contracts import execution_models as models
from speechnexus_contracts.execution import parse
from starlette.concurrency import run_in_threadpool

from speechnexus_gateway.application.abort_execution import (
    AbortExecutionRequest,
    ExecutionAbortService,
)
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.recovery import submission_response
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmissionIdentity,
    SubmitRequest,
    derived_output_stream_id,
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
_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$"
_NO_DISPATCH_ADAPTER = (
    "No engine dispatch adapter is configured on this gateway; the submission was not recorded."
)
_NO_ABORT_ADAPTER = (
    "No engine abort adapter is configured on this gateway; the execution state is unchanged."
)


def submit_request_from_payload(payload: models.SubmitExecutionRequest) -> SubmitRequest:
    """Translate one frozen wire request into the domain request, rejecting bad values.

    The frozen request carries the output ceiling and sample rate but no stream name, so an
    attempt that emits PCM names its single logical output stream from its own identity.
    """
    try:
        deadline_at = datetime.fromisoformat(payload["deadline_at"])
    except ValueError as error:
        raise InvalidExecutionRequestError("deadline_at must be an RFC 3339 instant") from error
    sample_rate_hz = payload["sample_rate_hz"]
    try:
        identity = SubmissionIdentity(
            submit_intent_id=payload["submit_intent_id"],
            lease_id=payload["lease_id"],
            operation_id=payload["operation_id"],
            attempt_id=payload["attempt_id"],
            stage_id=payload["stage_id"],
            workspace_id=payload["workspace_id"],
            pool_id=payload["pool_id"],
            host_node_id=payload["host_node_id"],
            gateway_id=payload["gateway_id"],
            instance_id=payload["instance_id"],
            instance_generation=payload["instance_generation"],
            binding_revision_id=payload["binding_revision_id"],
            policy_revision_id=payload["policy_revision_id"],
            voice_revision_id=payload["voice_revision_id"],
            fencing_token=payload["fencing_token"],
            command_digest=payload["command_digest"],
            deadline_at=deadline_at,
            max_output_audio_ms=payload["max_output_audio_ms"],
            sample_rate_hz=sample_rate_hz,
        )
        return SubmitRequest(
            identity=identity,
            command=cast(Any, payload["command"]),
            output_stream_id=(
                None
                if sample_rate_hz is None
                else derived_output_stream_id(identity.operation_id, identity.attempt_id)
            ),
        )
    except ValueError as error:
        raise InvalidExecutionRequestError(f"the execution request is invalid: {error}") from error


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
        outcome = await run_in_threadpool(service.submit, submit_request_from_payload(payload))
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

    return router
