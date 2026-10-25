"""C-owned executable version routes, injected into production only by its owner.

The app factory is useful for independent deployments and real HTTP qualification;
it does not silently enable workbench in the shared production composition root.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute, iter_route_contexts
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
    "internal_error": (500, "api_error", "Workbench request could not be completed."),
}


def _error(code: str, rid: str, operation: str | None) -> JSONResponse:
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
                "retryable": False,
            }
        },
    )
    headers = {"X-Request-ID": rid, "Cache-Control": "no-store", "X-Should-Retry": "false"}
    if operation is not None:
        validate_response(operation, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


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
            except Exception:
                # CancelledError is a BaseException and must reach the ASGI host.
                return _error("internal_error", rid, operation)

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
