"""Three generated HTTP operations over the private export snapshot.

Cancelling is idempotent by state: a repeated cancellation is accepted without
work, and a finished export reports a conflict instead of pretending to cancel.
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.exports import WorkbenchExports
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.exports import ExportStateConflictError
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.interfaces.export_views import export_job_payload
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    read_envelope,
    request_id,
    validate_limits,
)
from speechnexus_backend.workbench.interfaces.http_versions import _error

OPERATIONS = frozenset({"createBookExport", "getBookExport", "cancelBookExport"})


def create_export_router(
    service: WorkbenchExports | None,
    *,
    default_workspace_id: str,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> APIRouter:
    validate_limits(max_json_bytes, body_timeout_seconds)
    parse("ExportCreateRequest", cast(Any, {"expected_casting_revision": "1", "format": "zip"}))
    document = contract()
    router = APIRouter()

    async def dispatch(operation: str, value: dict[str, Any]) -> tuple[int, Any]:
        if service is None:
            raise IngressError("workbench_unavailable")
        path = value["path"]
        if operation == "getBookExport":
            return 200, export_job_payload(
                await service.get_export(
                    export_id=path["export_id"], workspace_id=default_workspace_id
                )
            )
        if operation == "cancelBookExport":
            export, changed = await service.cancel_export(
                export_id=path["export_id"], workspace_id=default_workspace_id
            )
            # 200 means this call cancelled a live export; an already settled one is
            # accepted without work, which keeps repeated cancellation idempotent.
            return (200 if changed else 202), export_job_payload(export)
        body = value["body"]
        return 202, export_job_payload(
            await service.create_export(
                workspace_id=default_workspace_id,
                book_id=path["book_id"],
                export_format=body["format"],
                expected_casting_revision=int(body["expected_casting_revision"]),
                unit_ids=body.get("unit_ids"),
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
            except ExportStateConflictError:
                return _error("invalid_change", rid, operation)
            except InvalidVersionChangeError:
                return _error("invalid_change", rid, operation)
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
