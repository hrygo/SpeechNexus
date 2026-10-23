"""Store ambiguity is a query/reconcile instruction, never blind execution retry."""

from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from speechnexus_contracts.execution import parse

from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionRejectedError,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
)

type Recovery = Literal[
    "none", "retry_transaction", "query_original", "query_current_authority", "reconcile"
]


def _response(code: str, message: str, status: int, recovery: Recovery = "none") -> JSONResponse:
    payload = parse(
        "ExecutionControlErrorResponse",
        {
            "error": {
                "code": code,
                "message": message,
                "retryable": recovery == "retry_transaction",
                "recovery": recovery,
            }
        },
    )
    return JSONResponse(status_code=status, content=payload)


def install_control_error_handlers(app: FastAPI) -> None:
    async def rejected(_request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, ExecutionRejectedError):
            raise error
        code = error.error_code
        if code is ExecutionErrorCode.COMPONENT_CREDENTIAL_INVALID:
            return _response(code.value, "The gateway component credential is invalid.", 401)
        if code is ExecutionErrorCode.INVALID_EXECUTION_REQUEST:
            return _response(code.value, str(error), 422)
        return _response(code.value, str(error), 409, "query_current_authority")

    async def unavailable(_request: Request, error: Exception) -> JSONResponse:
        if isinstance(error, SubmissionOutcomeUnknownError):
            return _response(
                "control_outcome_unknown",
                "The control transaction outcome is unknown; query the original identity.",
                503,
                "query_original",
            )
        if not isinstance(error, SubmissionStoreError):
            raise error
        if error.code == "idempotency_conflict":
            return _response(
                "control_state_conflict",
                "The original control identity already has another fact.",
                409,
                "query_current_authority",
            )
        if error.recovery == "retry_transaction":
            return _response(
                "control_store_unavailable",
                "The control operation could not complete within its bounded wait.",
                503,
                "retry_transaction",
            )
        return _response(
            "control_reconciliation_required",
            "The control store requires reconciliation; no success is confirmed.",
            503,
            "reconcile",
        )

    async def invalid(_request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, RequestValidationError):
            raise error
        # Do not serialize Pydantic's input/context, which can contain credentials/content.
        return _response("invalid_execution_request", "The control request is invalid.", 422)

    app.add_exception_handler(ExecutionRejectedError, rejected)
    app.add_exception_handler(SubmissionStoreError, unavailable)
    app.add_exception_handler(SubmissionOutcomeUnknownError, unavailable)
    app.add_exception_handler(RequestValidationError, invalid)
