"""Stable HTTP mapping of runtime-control refusals.

Every refusal the contract enumerates has exactly one status here, and an
unmapped code fails the unit tests instead of silently becoming a 500.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from speechnexus_node_controller.domain.intents import (
    InvalidRuntimeRequestError,
    RuntimeControlErrorCode,
    RuntimeControlRejectedError,
)
from speechnexus_node_controller.interfaces.http.schemas import (
    RuntimeControlErrorDetail,
    RuntimeControlErrorResponse,
)

_STATUS_BY_CODE: Final[dict[RuntimeControlErrorCode, int]] = {
    RuntimeControlErrorCode.COMPONENT_CREDENTIAL_INVALID: status.HTTP_401_UNAUTHORIZED,
    RuntimeControlErrorCode.RUNTIME_INTENT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    RuntimeControlErrorCode.TARGET_NODE_MISMATCH: status.HTTP_409_CONFLICT,
    RuntimeControlErrorCode.STALE_FENCING_TOKEN: status.HTTP_409_CONFLICT,
    RuntimeControlErrorCode.STALE_INSTANCE_GENERATION: status.HTTP_409_CONFLICT,
    RuntimeControlErrorCode.GATEWAY_DRAIN_INCOMPLETE: status.HTTP_409_CONFLICT,
    RuntimeControlErrorCode.INTENT_CONFLICT: status.HTTP_409_CONFLICT,
    RuntimeControlErrorCode.UNAPPROVED_RUNTIME_TEMPLATE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RuntimeControlErrorCode.INVALID_RUNTIME_REQUEST: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RuntimeControlErrorCode.RUNTIME_CONTROLLER_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def status_for(error: RuntimeControlRejectedError) -> int:
    """Map one refusal onto its contract status."""
    return _STATUS_BY_CODE[error.error_code]


def body_for(error: RuntimeControlRejectedError) -> RuntimeControlErrorResponse:
    return RuntimeControlErrorResponse(
        error=RuntimeControlErrorDetail(
            code=error.error_code,
            message=str(error) or error.error_code.value,
            retryable=error.retryable,
            observed_fencing_token=error.observed_fencing_token,
        )
    )


def response_for(error: RuntimeControlRejectedError) -> JSONResponse:
    body = body_for(error)
    return JSONResponse(status_code=status_for(error), content=body.model_dump(mode="json"))


def invalid_request_response(message: str) -> JSONResponse:
    return response_for(InvalidRuntimeRequestError(message))


def install_error_handlers(app: FastAPI) -> None:
    """Expose refusals with the contract error envelope instead of FastAPI defaults."""

    async def _handle_rejected(_request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, RuntimeControlRejectedError):
            raise error
        return response_for(error)

    async def _handle_validation(_request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, RequestValidationError):
            raise error
        locations = sorted(
            ".".join(str(part) for part in failure.get("loc", ())) for failure in error.errors()
        )
        detail = ", ".join(location for location in locations if location)
        return invalid_request_response(
            f"request validation failed: {detail}" if detail else "request validation failed"
        )

    app.add_exception_handler(RuntimeControlRejectedError, _handle_rejected)
    app.add_exception_handler(RequestValidationError, _handle_validation)
