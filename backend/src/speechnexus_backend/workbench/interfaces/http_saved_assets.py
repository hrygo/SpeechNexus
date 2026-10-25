"""Two generated HTTP operations over independently committed saved assets."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.saved_assets import (
    ObjectZoneUnavailableError,
    WorkbenchSavedAssets,
)
from speechnexus_backend.workbench.application.speech_sources import SpeechSourceError
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    read_envelope,
    request_id,
    validate_limits,
)
from speechnexus_backend.workbench.interfaces.http_versions import _error
from speechnexus_backend.workbench.interfaces.saved_asset_views import saved_asset_payload

OPERATIONS = frozenset({"getSavedAsset", "saveProjectAsset"})


def create_saved_asset_router(
    service: WorkbenchSavedAssets | None,
    *,
    default_workspace_id: str,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> APIRouter:
    validate_limits(max_json_bytes, body_timeout_seconds)
    parse(
        "SavedAssetCreateRequest",
        {"source_artifact_id": "artifact-placeholder", "source_receipt_id": "receipt-placeholder"},
    )
    document = contract()
    router = APIRouter()

    async def dispatch(operation: str, value: dict[str, Any]) -> tuple[int, Any]:
        if service is None:
            raise IngressError("workbench_unavailable")
        if operation == "getSavedAsset":
            return 200, saved_asset_payload(
                await service.get(
                    asset_id=value["path"]["asset_id"], workspace_id=default_workspace_id
                )
            )
        body = value["body"]
        return 201, saved_asset_payload(
            await service.save(
                project_id=value["path"]["project_id"],
                workspace_id=default_workspace_id,
                source_artifact_id=body["source_artifact_id"],
                source_receipt_id=body["source_receipt_id"],
                display_name=body.get("display_name"),
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
            except InvalidVersionChangeError:
                return _error("invalid_change", rid, operation)
            except SpeechSourceError as error:
                return _error(error.code, rid, operation)
            except ObjectZoneUnavailableError:
                return _error("store_unavailable", rid, operation)
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
