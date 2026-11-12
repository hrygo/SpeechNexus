"""Recorded release-evidence identity reads; no document body and no locator.

Both reads expose only facts the publication evidence table records: the
evidence identifier, the document digest when a publication row recorded one,
and the earliest recording time. The table has no release-evidence locator
column, so this response never carries ``source`` and the contract marks that
field optional. A store that cannot be read answers 503 instead of 404, because
absence is only proven after a successful read.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin_http import validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.binding_evidence import (
    BindingEvidenceService,
    BindingEvidenceUnavailableError,
)
from speechnexus_backend.speech.domain.binding_evidence import RecordedEvidenceIdentity

CAPACITY = "getCapacityManifest"
QUALIFICATION = "getQualificationReport"
CAPACITY_PATH = "/v1/nexus/admin/capacity-manifests/{manifest_id}"
QUALIFICATION_PATH = "/v1/nexus/admin/qualification-reports/{report_id}"

_ERRORS = {
    "invalid_binding_request": (400, "invalid_request_error", "Invalid binding request.", False),
    "binding_evidence_not_found": (
        404,
        "not_found_error",
        "No recorded publication evidence names this document.",
        False,
    ),
    "binding_unavailable": (
        503,
        "service_unavailable_error",
        "Publication evidence is unavailable.",
        True,
    ),
}


def evidence_error(code: str, request_id: str, operation: str) -> JSONResponse:
    if code not in _ERRORS:
        code = "binding_unavailable"
    status, kind, message, retryable = _ERRORS[code]
    body = {
        "error": {
            "code": code,
            "type": kind,
            "message": message,
            "param": None,
            "request_id": request_id,
            "retryable": retryable,
        }
    }
    validate_response(operation, status, {"body": body})
    return JSONResponse(
        body,
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Should-Retry": str(retryable).lower()},
    )


def stamp(recorded_at_us: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=recorded_at_us)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def evidence_body(recorded: RecordedEvidenceIdentity) -> dict[str, object]:
    """Project only recorded facts; the digest is omitted when none was recorded."""
    body: dict[str, object] = {
        "id": recorded.identity,
        "recorded_at": stamp(recorded.recorded_at_us),
    }
    if recorded.digest is not None:
        body["digest"] = recorded.digest
    return body


_PATH_PARAMETER = {CAPACITY: "manifest_id", QUALIFICATION: "report_id"}


def envelope_for(
    operation: str, identity: str, query: list[tuple[str, str]], request_id: str
) -> dict[str, object]:
    """Build the declared request envelope; unknown or duplicate query keys fail."""
    if len({key for key, _ in query}) != len(query):
        raise ContractValidationError("Ambiguous query")
    return {
        "path": {_PATH_PARAMETER[operation]: identity},
        "query": dict(query),
        "headers": {"x-request-id": request_id},
    }


def _read(
    service: BindingEvidenceService,
    operation: str,
    identity: str,
    envelopes: dict[str, object],
    request_id: str,
) -> Response:
    try:
        validate_request(operation, envelopes)
    except ContractValidationError, ValueError, TypeError, RecursionError:
        return evidence_error("invalid_binding_request", request_id, operation)
    try:
        recorded = (
            service.capacity_manifest(identity)
            if operation == CAPACITY
            else service.qualification_report(identity)
        )
    except BindingEvidenceUnavailableError:
        return evidence_error("binding_unavailable", request_id, operation)
    if recorded is None:
        return evidence_error("binding_evidence_not_found", request_id, operation)
    body = evidence_body(recorded)
    validate_response(operation, 200, {"body": body, "headers": {"X-Request-ID": request_id}})
    return JSONResponse(content=body, headers={"Cache-Control": "no-store"})


def create_binding_evidence_router(
    get_service: Callable[[], BindingEvidenceService | None],
) -> APIRouter:
    router = APIRouter(tags=["model-bindings"])

    async def dispatch(request: Request, operation: str) -> Response:
        request_id = str(request.state.request_id)
        service = get_service()
        if service is None:
            return evidence_error("binding_unavailable", request_id, operation)
        identity = request.path_params[_PATH_PARAMETER[operation]]
        try:
            envelope = envelope_for(
                operation, identity, list(request.query_params.multi_items()), request_id
            )
        except ContractValidationError:
            return evidence_error("invalid_binding_request", request_id, operation)
        return await run_in_threadpool(_read, service, operation, identity, envelope, request_id)

    @router.get(CAPACITY_PATH, operation_id=CAPACITY)
    async def capacity_manifest(request: Request) -> Response:
        return await dispatch(request, CAPACITY)

    @router.get(QUALIFICATION_PATH, operation_id=QUALIFICATION)
    async def qualification_report(request: Request) -> Response:
        return await dispatch(request, QUALIFICATION)

    return router
