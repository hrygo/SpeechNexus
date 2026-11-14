"""C-owned executable version routes, injected into production only by its owner.

The app factory is useful for independent deployments and real HTTP qualification;
it does not silently enable workbench in the shared production composition root.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute, iter_route_contexts
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response
from starlette.routing import Match
from starlette.types import Lifespan

from speechnexus_backend.workbench.application.transcript_sources import TranscriptSourceError
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    read_envelope,
    request_id,
    validate_limits,
)
from speechnexus_backend.workbench.interfaces.project_commands import ProjectCommandBoundary
from speechnexus_backend.workbench.interfaces.transcript_commands import TranscriptCommandBoundary
from speechnexus_backend.workbench.interfaces.version_views import (
    project_payload,
    revision_payload,
    transcript_view,
)

_log = logging.getLogger(__name__)

OPERATIONS = frozenset(
    {
        "createProject",
        "getProject",
        "updateProject",
        "createTranscript",
        "getTranscript",
        "createTranscriptRevision",
        "getTranscriptRevision",
    }
)

# Stable codes/messages; neither request data nor exception details enter an error.
ERRORS = {
    "range_not_satisfiable": (
        416,
        "invalid_request_error",
        "Requested byte range is not satisfiable.",
    ),
    "download_not_ready": (409, "invalid_request_error", "Export content is not ready."),
    "download_expired": (410, "invalid_request_error", "Export content has expired."),
    "download_too_large": (
        413,
        "invalid_request_error",
        "Content exceeds the deployment download limit.",
    ),
    "download_unavailable": (503, "service_unavailable_error", "Download content is unavailable."),
    "meetings_unavailable": (503, "service_unavailable_error", "Meetings are unavailable."),
    "meeting_source_unsupported": (
        422,
        "invalid_request_error",
        "Meeting source or audio persistence is unavailable.",
    ),
    "minutes_conflict": (409, "invalid_request_error", "Minutes task state conflicts."),
    "llm_disabled": (503, "service_unavailable_error", "Text LLM is disabled."),
    "llm_profile_mismatch": (422, "invalid_request_error", "Text LLM profile does not match."),
    "llm_invalid_input": (422, "invalid_request_error", "Text LLM input is invalid."),
    "llm_source_not_sealed": (409, "invalid_request_error", "Minutes source is not sealed."),
    "llm_source_changed": (409, "invalid_request_error", "Minutes source has changed."),
    "llm_source_unavailable": (503, "service_unavailable_error", "Minutes source is unavailable."),
    "llm_unavailable": (503, "service_unavailable_error", "Text LLM is unavailable."),
    "llm_invalid_response": (503, "service_unavailable_error", "Text LLM response is invalid."),
    "invalid_cursor": (400, "invalid_request_error", "Invalid pagination cursor."),
    "ordinal_conflict": (
        409,
        "invalid_request_error",
        "Chapter ordinal is already used in this book.",
    ),
    "deletions_unavailable": (503, "service_unavailable_error", "Deletion is unavailable."),
    "voice_catalogue_unavailable": (
        503,
        "service_unavailable_error",
        "Voice catalogue is unavailable.",
    ),
    "deletion_owner_mismatch": (422, "invalid_request_error", "Wrong deletion owner."),
    "deletion_resource_unsupported": (422, "invalid_request_error", "Unsupported deletion kind."),
    "deletion_confirmation_mismatch": (422, "invalid_request_error", "Confirmation mismatch."),
    "deletion_plan_expired": (409, "invalid_request_error", "Deletion plan has expired."),
    "deletion_plan_changed": (409, "invalid_request_error", "Deletion dependencies changed."),
    "deletion_dependency_conflict": (409, "invalid_request_error", "Blocking deletion dependency."),
    "malformed_request": (400, "invalid_request_error", "Malformed request."),
    "invalid_request": (400, "invalid_request_error", "Request does not match the contract."),
    "request_timeout": (408, "invalid_request_error", "Request body deadline exceeded."),
    "request_too_large": (
        413,
        "invalid_request_error",
        "Request body exceeds the configured limit.",
    ),
    "unsupported_media_type": (415, "invalid_request_error", "UTF-8 application/json is required."),
    "resource_not_found": (404, "not_found_error", "Workbench resource not found."),
    "method_not_allowed": (405, "invalid_request_error", "HTTP method is not supported."),
    "revision_conflict": (409, "invalid_request_error", "Resource revision changed."),
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "Idempotency key has a different intent.",
    ),
    "invalid_change": (422, "invalid_request_error", "Invalid version change."),
    "source_invalid": (
        422,
        "invalid_request_error",
        "Speech source identity or content is invalid.",
    ),
    "source_unsupported": (422, "invalid_request_error", "Speech source format is unsupported."),
    "source_too_large": (
        413,
        "invalid_request_error",
        "Speech source exceeds the configured limit.",
    ),
    "source_expired": (410, "invalid_request_error", "Speech source has expired."),
    "source_unavailable": (503, "service_unavailable_error", "Speech source is unavailable."),
    "workbench_unavailable": (503, "service_unavailable_error", "Workbench is unavailable."),
    "store_unavailable": (503, "service_unavailable_error", "Workbench storage is unavailable."),
    "outcome_unknown": (
        503,
        "service_unavailable_error",
        "Mutation outcome is unknown. Preserve the original idempotency key; "
        "do not create a new intent.",
    ),
    "render_attempt_conflict": (
        409,
        "invalid_request_error",
        "A render attempt is already running for this unit.",
    ),
    # The dispatch refusal vocabulary, keyed to the operator step each one demands
    # rather than to the upstream Job code that reports it. `render_submission_rejected`
    # stays the fallback: a local contract refusal, or an upstream refusal whose
    # envelope named no cause this workbench can act on.
    "render_submission_text_too_long": (
        422,
        "invalid_request_error",
        "The unit text exceeds the one-request inline bound; split it into units "
        "within that bound.",
    ),
    "render_submission_field_unsupported": (
        422,
        "invalid_request_error",
        "A pinned field value is not supported by this Job request.",
    ),
    "render_submission_workspace_not_configured": (
        422,
        "invalid_request_error",
        "The workspace has no provisioned Job quota; an operator must provision it.",
    ),
    "render_submission_workspace_unavailable": (
        422,
        "invalid_request_error",
        "The workspace does not exist or is disabled; select another workspace.",
    ),
    "render_submission_voice_unavailable": (
        422,
        "invalid_request_error",
        "The pinned voice revision is not published; publish it before dispatching.",
    ),
    "render_submission_binding_unavailable": (
        422,
        "invalid_request_error",
        "The pinned model binding or profile revision is not published; "
        "publish it or pin a published revision.",
    ),
    "render_submission_asset_unavailable": (
        422,
        "invalid_request_error",
        "The pinned asset is not ready; wait for readiness or upload it again.",
    ),
    "render_submission_body_too_large": (
        422,
        "invalid_request_error",
        "The derived Job request body exceeds the deployment receive limit; shorten the unit text.",
    ),
    "render_submission_rejected": (
        422,
        "invalid_request_error",
        "The pinned render unit cannot be dispatched as a speech Job.",
    ),
    # The refusal status families whose meaning is carried by the status, not by the
    # upstream code. Each is a distinct next step for the operator, so none of them
    # may be folded into the 422 "the request is invalid" bucket: a 401 is a broken
    # deployment credential, a 403 a permission refusal, a 415 a media-type refusal,
    # a 429 an admission refusal that wrote no row and asks for a delay.
    "render_submission_credentials_rejected": (
        401,
        "authentication_error",
        "The speech service rejected the deployment credential.",
    ),
    "render_submission_permission_denied": (
        403,
        "permission_error",
        "This deployment is not permitted to submit the Job.",
    ),
    "render_submission_media_unsupported": (
        415,
        "invalid_request_error",
        "The refusing layer does not accept the JSON media type this dispatch sends.",
    ),
    # The three 409 meanings share one status class and differ only in the operator
    # step: a key that is already spent on another intent, a previous execution that
    # has not confirmed its stop (the one conflict that is worth retrying), and
    # everything else. Only the middle one is retryable.
    "render_submission_idempotency_conflict": (
        409,
        "invalid_request_error",
        "The Idempotency-Key already names another submission intent; inspect that "
        "Job or dispatch under a new key.",
    ),
    "render_submission_cleanup_pending": (
        409,
        "invalid_request_error",
        "The unit's previous execution is not confirmed stopped; retry after cleanup.",
    ),
    "render_submission_conflict": (
        409,
        "invalid_request_error",
        "The refusing layer reported a conflict this workbench cannot attribute to one step.",
    ),
    # A 429 proves "not now", not "why": it may be exhausted admission capacity or a
    # limiter in front of it, so both the name and the message stop short of a cause
    # nobody read (#755's attribution rule applied to a status-only observation).
    "render_submission_rate_limited": (
        429,
        "rate_limit_error",
        "The refusing layer declined this dispatch (rate limit or exhausted capacity) "
        "and created no Job; retry after the stated delay.",
    ),
    "internal_error": (500, "api_error", "Workbench request could not be completed."),
}

# The stable text-LLM dispositions, keyed by the private code the LLM port raises. Both
# readers of that port (assistant turns and minutes) project through this one table:
# anything unrecognised reports as `llm_unavailable` rather than inventing a public code
# or, worse, building a table key that does not exist (#777's out-of-range reproduction).
LLM_CODES = {
    "disabled": "llm_disabled",
    "invalid_input": "llm_invalid_input",
    "profile_mismatch": "llm_profile_mismatch",
    "unavailable": "llm_unavailable",
    "invalid_response": "llm_invalid_response",
    "source_not_sealed": "llm_source_not_sealed",
    "source_changed": "llm_source_changed",
    "source_unavailable": "llm_source_unavailable",
}


def _error(
    code: str,
    rid: str,
    operation: str | None,
    *,
    content_range: str | None = None,
    param: str | None = None,
    retryable: bool = False,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    """Answer one refusal without depending on the operation's response declarations.

    The declared envelope is built and checked first. When an operation never declared
    that response — no ``500`` at all, or any ``400``/``408``/``413``/``415``/``503`` it
    omits — the check refuses the envelope, and letting
    that refusal escape replaces the caller's only machine-readable answer with a bare
    ``text/plain`` 500 from the ASGI host. The same refusal is then rebuilt from the
    static table alone: same status, ``code`` and correlation, still validated against
    ``ErrorResponse``, only the per-operation declaration gate is skipped. Structure
    stays a guarantee of this layer; the declaration mismatch is logged, never echoed.

    Every caller names a code from the table above, and a code that is not in it is a
    defect in this layer, not a refusal: reporting it as ``internal_error`` keeps this
    entry point total. A dynamic code (one forwarded from a port as-is) must never be
    able to leave the handler as a ``KeyError``, which is the same bare 500 this
    function exists to prevent.
    """
    if code not in ERRORS:
        _log.error(
            "unknown workbench error code; answering internal_error code=%r operation=%s",
            code,
            operation,
        )
        code = "internal_error"
    try:
        return _declared_error(
            code,
            rid,
            operation,
            content_range=content_range,
            param=param,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )
    except ContractValidationError as failure:
        _log.warning(
            "workbench error response outside the operation's declarations; "
            "answering with the declaration-independent envelope "
            "operation=%s code=%s request_id=%s failure=%r",
            operation,
            code,
            rid,
            failure,
        )
        return _minimal_error(code, rid, retryable=retryable)


def _declared_error(
    code: str,
    rid: str,
    operation: str | None,
    *,
    content_range: str | None = None,
    param: str | None = None,
    retryable: bool = False,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    status, kind, message = ERRORS[code]
    error: dict[str, object] = {
        "code": code,
        "type": kind,
        "message": message,
        "param": param,
        "request_id": rid,
        # The retry permission is the disposition's, not the status layer's: a 409
        # cleanup wait may be retried and a 401 may not, whatever their status code
        # suggests. Only a disposition that says "retry" passes this as true.
        "retryable": retryable,
    }
    if retry_after_seconds is not None:
        # The delayed answer states the same fact twice, in the unit this protocol
        # declares for each: seconds on the header, milliseconds in the envelope.
        error["retry_after_ms"] = retry_after_seconds * 1000
    body = parse("ErrorResponse", {"error": error})
    headers = {
        "X-Request-ID": rid,
        "Cache-Control": "no-store",
        "X-Should-Retry": str(retryable).lower(),
    }
    if content_range is not None:
        headers["Content-Range"] = content_range
    if retry_after_seconds is not None:
        headers["Retry-After"] = str(retry_after_seconds)
    if operation is not None:
        validate_response(operation, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


def _minimal_error(code: str, rid: str, *, retryable: bool = False) -> JSONResponse:
    """The declaration-independent envelope, used only when the gate refused the other.

    It carries the fields ``ErrorResponse`` requires and nothing taken from the request
    or from an operation's declarations: the stable status/``code``/``type``/``message``
    from the table above, a null ``param``, the caller's ``request_id`` and the retry
    permission. Optional facts an operation never declared are not claimed either — the
    offending ``param``, an advertised wait or a content range — because a refusal the
    operation does not list has no declared detail to fill them with. Building it
    independently keeps the last resort intact when the richer envelope's extra facts
    are what failed validation, and ``parse`` keeps the structural guarantee: skipping
    the declaration gate may not cost a valid error body.

    The dropped ``retry_after_ms`` is unreachable with the current table rather than
    lost: only two dispositions are retryable (the 429 admission refusal and the 409
    cleanup wait), both are raised by the routed render submission operation
    (``generateRenderUnitV2``), and that operation declares 429 and 409. A future
    retryable disposition must therefore declare its status on every operation that can
    emit it -- otherwise this fallback would answer ``retryable: true`` with no delay,
    telling the client to retry without saying when.
    """
    status, kind, message = ERRORS[code]
    body = parse(
        "ErrorResponse",
        {
            "error": {
                "code": code,
                "type": kind,
                "message": message,
                "param": None,
                "request_id": rid,
                "retryable": retryable,
            }
        },
    )
    return JSONResponse(
        body,
        status_code=status,
        headers={
            "X-Request-ID": rid,
            "Cache-Control": "no-store",
            "X-Should-Retry": str(retryable).lower(),
        },
    )


def _unexpected_error(error: BaseException, rid: str, operation: str) -> JSONResponse:
    """Last-resort 500 for an exception no branch named: record the cause, never return it.

    The original failure is the only thing that can explain a 500 the deployment did not
    classify, so it is logged with its ``repr``; the envelope still states the stable
    ``internal_error`` refusal and never the exception's text.
    """
    _log.error(
        "unhandled workbench failure operation=%s request_id=%s error=%r",
        operation,
        rid,
        error,
        exc_info=error,
    )
    return _error("internal_error", rid, operation)


async def _dispatch(
    operation: str,
    value: dict[str, Any],
    service: WorkbenchVersions,
    workspace: str,
) -> tuple[int, Any]:
    path, query = value["path"], value["query"]
    key = value["headers"].get("idempotency-key")
    projects = ProjectCommandBoundary(service, default_workspace_id=workspace)
    transcripts = TranscriptCommandBoundary(service)
    if operation == "createProject":
        return 201, project_payload(
            await projects.create_project(value["body"], idempotency_key=key)
        )
    if operation == "getProject":
        return 200, project_payload(await service.get_project(path["project_id"]))
    if operation == "updateProject":
        return 200, project_payload(
            await projects.update_project(path["project_id"], value["body"], idempotency_key=key)
        )
    if operation == "createTranscript":
        return 201, transcript_view(
            await transcripts.create_transcript(
                path["project_id"], value["body"], idempotency_key=key
            )
        )
    if operation == "createTranscriptRevision":
        result = await transcripts.revise_transcript(
            path["transcript_id"], value["body"], idempotency_key=key
        )
        return 201, revision_payload(result.head)
    revision = path.get("revision", query.get("revision"))
    current, selected = await service.get_transcript_view(
        path["transcript_id"], None if revision is None else int(revision)
    )
    if operation == "getTranscriptRevision":
        return 200, revision_payload(selected)
    return 200, transcript_view(current, selected)


def create_version_router(
    service: WorkbenchVersions | None,
    *,
    default_workspace_id: str,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> APIRouter:
    validate_limits(max_json_bytes, body_timeout_seconds)
    # Deployment configuration is validated using the same generated identifier field.
    from speechnexus_contracts.workbench import parse as parse_body

    parse_body(
        "ProjectCreateRequest",
        {"name": "configuration-check", "workspace_id": default_workspace_id},
    )
    router = APIRouter()

    def endpoint(operation: str, has_body: bool) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            rid = request_id(request)
            try:
                value = await read_envelope(
                    request,
                    operation,
                    has_body=has_body,
                    max_json_bytes=max_json_bytes,
                    body_timeout_seconds=body_timeout_seconds,
                )
                if service is None:
                    return _error("workbench_unavailable", rid, operation)
                status, body = await _dispatch(operation, value, service, default_workspace_id)
                headers = {"X-Request-ID": rid, "Cache-Control": "no-store"}
                validate_response(operation, status, {"body": body, "headers": headers})
                return JSONResponse(body, status_code=status, headers=headers)
            except IngressError as error:
                return _error(error.code, rid, operation)
            except ResourceNotFoundError:
                return _error("resource_not_found", rid, operation)
            except RevisionConflictError:
                return _error("revision_conflict", rid, operation)
            except IdempotencyConflictError:
                return _error("idempotency_conflict", rid, operation)
            except InvalidVersionChangeError:
                return _error("invalid_change", rid, operation)
            except TranscriptSourceError as error:
                return _error(error.code, rid, operation)
            except VersionStoreError as error:
                return _error(
                    "outcome_unknown" if error.commit_unknown else "store_unavailable",
                    rid,
                    operation,
                )
            except Exception as error:
                # CancelledError is a BaseException and must reach the ASGI host.
                return _unexpected_error(error, rid, operation)

        return handle

    for path, item in contract()["paths"].items():
        for method, operation in item.items():
            name = operation["operationId"]
            if name in OPERATIONS:
                router.add_api_route(
                    path,
                    endpoint(name, "requestBody" in operation),
                    methods=[method.upper()],
                    operation_id=name,
                    response_model=None,
                )
    return router


def create_workbench_app(
    service: WorkbenchVersions | None,
    *,
    default_workspace_id: str,
    max_json_bytes: int,
    body_timeout_seconds: float,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    class WorkbenchApp(FastAPI):
        def openapi(self) -> dict[str, Any]:
            source = contract()
            registered = {
                route.operation_id
                for route in iter_route_contexts(self.routes)
                if isinstance(route.original_route, APIRoute)
            }
            source["paths"] = {
                path: {method: op for method, op in item.items() if op["operationId"] in registered}
                for path, item in source["paths"].items()
            }
            source["paths"] = {path: item for path, item in source["paths"].items() if item}
            return copy.deepcopy(source)

    # FastAPI's default interactive docs fetch scripts/styles/fonts from public CDNs.
    # Preserve the local machine schema without introducing browser egress.
    app = WorkbenchApp(
        redirect_slashes=False,
        docs_url=None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
        lifespan=lifespan,
    )
    app.include_router(
        create_version_router(
            service,
            default_workspace_id=default_workspace_id,
            max_json_bytes=max_json_bytes,
            body_timeout_seconds=body_timeout_seconds,
        )
    )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        # No matched operation exists for an unknown route or unsupported method.
        response = _error(
            "method_not_allowed" if exc.status_code == 405 else "resource_not_found",
            request_id(request),
            None,
        )
        response.status_code = exc.status_code
        if exc.status_code == 405:
            response.headers.update(exc.headers or {})
            methods = {
                method
                for route in iter_route_contexts(app.routes)
                if isinstance(route.original_route, APIRoute)
                and route.matches(request.scope)[0] != Match.NONE
                for method in (route.methods or ())
            }
            if methods:
                response.headers["Allow"] = ", ".join(sorted(methods))
        return response

    return app
