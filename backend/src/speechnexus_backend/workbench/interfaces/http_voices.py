"""Generated read-only HTTP operations over the base's published voice catalogue."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.application.voices import (
    VoiceCatalogueUnavailableError,
    WorkbenchVoices,
)
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    default_limit,
    read_envelope,
    request_id,
    validate_limits,
)
from speechnexus_backend.workbench.interfaces.http_versions import _error
from speechnexus_backend.workbench.interfaces.voice_views import (
    voice_catalogue_payload,
    voice_revision_payload,
)

OPERATIONS = frozenset({"listVoices", "getVoiceRevision", "getVoiceRevisionV2"})


def create_voice_router(
    service: WorkbenchVoices | None,
    *,
    default_workspace_id: str,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> APIRouter:
    validate_limits(max_json_bytes, body_timeout_seconds)
    document = contract()
    limits = {
        op["operationId"]: default_limit(document, op)
        for item in document["paths"].values()
        for op in item.values()
        if op["operationId"] == "listVoices"
    }
    router = APIRouter()

    async def dispatch(operation: str, value: dict[str, Any]) -> tuple[int, Any]:
        if service is None:
            # The deployment has no approved speech base, so it must say the catalogue is
            # unavailable instead of reporting an empty one it cannot vouch for.
            raise IngressError("voice_catalogue_unavailable")
        path, query = value["path"], value["query"]
        if operation == "listVoices":
            limit = int(query["limit"]) if "limit" in query else limits[operation]
            return 200, voice_catalogue_payload(
                await service.list_voices(
                    workspace_id=str(query.get("workspace_id", default_workspace_id)),
                    language=query.get("language"),
                    limit=limit,
                    after=query.get("after"),
                )
            )
        body = voice_revision_payload(
            await service.get_revision(voice_id=path["voice_id"], revision=path["revision"])
        )
        # v1 remains a frozen 128-character projection for already deployed validators.
        # v2 preserves the complete public reason, including valid 129..2000 characters.
        if operation == "getVoiceRevision" and len(body["disabled_reason"] or "") > 128:
            raise VoiceCatalogueUnavailableError
        return 200, body

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
            except VoiceCatalogueUnavailableError:
                return _error("voice_catalogue_unavailable", rid, operation)
            except ValueError:
                # A query the frozen values refuse, before any call leaves the workbench.
                return _error("invalid_request", rid, operation)
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
