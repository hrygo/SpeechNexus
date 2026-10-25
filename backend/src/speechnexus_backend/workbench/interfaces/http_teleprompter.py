"""Two generated HTTP operations over the atomic project teleprompter state."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.teleprompter import WorkbenchTeleprompter
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
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
from speechnexus_backend.workbench.interfaces.http_versions import _error
from speechnexus_backend.workbench.interfaces.teleprompter_views import teleprompter_payload

OPERATIONS = frozenset({"getTeleprompterState", "updateTeleprompterState"})


def create_teleprompter_router(
    service: WorkbenchTeleprompter | None,
    *,
    default_workspace_id: str,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> APIRouter:
    validate_limits(max_json_bytes, body_timeout_seconds)
    parse(
        "TeleprompterUpdateRequest",
        {"script_revision": "1", "visible_position": 0, "manual_hold": False},
    )
    document = contract()
    router = APIRouter()

    async def dispatch(operation: str, value: dict[str, Any]) -> tuple[int, Any]:
        if service is None:
            raise IngressError("workbench_unavailable")
        project_id = value["path"]["project_id"]
        if operation == "getTeleprompterState":
            return 200, teleprompter_payload(
                await service.get(project_id=project_id, workspace_id=default_workspace_id)
            )
        body = value["body"]
        return 200, teleprompter_payload(
            await service.update(
                project_id=project_id,
                workspace_id=default_workspace_id,
                script_revision=int(body["script_revision"]),
                visible_position=body["visible_position"],
                manual_hold=body["manual_hold"],
                key=value["headers"].get("idempotency-key"),
            )
        )

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
                status, body = await dispatch(operation, value)
                headers = {"X-Request-ID": rid, "Cache-Control": "no-store"}
                validate_response(operation, status, {"body": body, "headers": headers})
                return JSONResponse(body, status_code=status, headers=headers)
            except IngressError as error:
                return _error(error.code, rid, operation)
            except ResourceNotFoundError:
                return _error("resource_not_found", rid, operation)
            except IdempotencyConflictError:
                return _error("idempotency_conflict", rid, operation)
            except RevisionConflictError:
                return _error("revision_conflict", rid, operation)
            except InvalidVersionChangeError:
                return _error("invalid_change", rid, operation)
            except VersionStoreError as error:
                return _error(
                    "outcome_unknown" if error.commit_unknown else "store_unavailable",
                    rid,
                    operation,
                )
            except Exception:
                return _error("internal_error", rid, operation)

        return handle

    for path, item in document["paths"].items():
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
