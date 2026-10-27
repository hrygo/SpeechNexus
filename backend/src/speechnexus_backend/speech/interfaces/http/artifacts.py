"""Only implemented public Artifact operations; no storage identity crosses the wire."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from itertools import chain
from typing import Any

import rfc8785
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from speechnexus_backend.speech.application.artifacts import ArtifactService
from speechnexus_backend.speech.application.capability_snapshot import matches_if_none_match
from speechnexus_backend.speech.domain.artifacts import ArtifactError, ArtifactRecord
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.interfaces.http.artifact_contract import public_artifact
from speechnexus_backend.speech.interfaces.http.artifact_errors import artifact_error

PREFIX = "/v1/nexus/artifacts"
_CACHE_HEADERS = {"Cache-Control": "no-store"}
_ARTIFACT_ID_PARAMETER = {
    "name": "artifact_id",
    "in": "path",
    "required": True,
    "schema": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$",
        "minLength": 3,
        "maxLength": 128,
        "description": (
            "Opaque stable resource identifier. Prefixes are descriptive, not authorization claims."
        ),
    },
}
_REQUEST_ID_PARAMETER = {
    "name": "X-Request-ID",
    "in": "header",
    "required": False,
    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
}
_RANGE_PARAMETER = {
    "name": "Range",
    "in": "header",
    "required": False,
    "schema": {"type": "string", "pattern": "^bytes=[0-9]*-[0-9]*$"},
}
_METADATA_PARAMETERS = [_ARTIFACT_ID_PARAMETER, _REQUEST_ID_PARAMETER]
_CONTENT_PARAMETERS = [_ARTIFACT_ID_PARAMETER, _RANGE_PARAMETER, _REQUEST_ID_PARAMETER]


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _artifact_id(request: Request) -> str:
    value = request.path_params.get("artifact_id")
    if not isinstance(value, str):
        raise ValueError("Invalid artifact identifier")
    identifier(value)
    return value


def _metadata_etag(artifact: ArtifactRecord) -> str:
    payload: Any = public_artifact(artifact)
    digest = hashlib.sha256(rfc8785.dumps(payload)).hexdigest()
    return f'"sha256:{digest}"'


class _ArtifactContentResponse(StreamingResponse):
    def __init__(self, content: Iterable[bytes], close: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._close = close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await run_in_threadpool(self._close)


def create_artifact_router(get_service: Callable[[], ArtifactService | None]) -> APIRouter:
    router = APIRouter(tags=["artifacts"])

    @router.get(
        PREFIX + "/{artifact_id}",
        operation_id="getArtifact",
        summary="Get committed artifact metadata",
        openapi_extra={"parameters": _METADATA_PARAMETERS},
        responses={
            200: {"description": "Artifact metadata."},
            401: {"description": "Shared service credential required or invalid."},
            404: {"description": "Resource not found or not visible to the requested workspace."},
            410: {"description": "Content or event history has expired and is unavailable."},
        },
    )
    async def get_artifact(request: Request) -> Response:
        request_id = _request_id(request)
        service = get_service()
        if service is None:
            return artifact_error("artifact_service_unavailable", request_id)
        try:
            artifact = await run_in_threadpool(service.metadata, _artifact_id(request))
        except ValueError:
            return artifact_error("artifact_not_found", request_id)
        except ArtifactError as error:
            return artifact_error(error.code, request_id)
        etag = _metadata_etag(artifact)
        if matches_if_none_match(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers={**_CACHE_HEADERS, "ETag": etag})
        return JSONResponse(public_artifact(artifact), headers={**_CACHE_HEADERS, "ETag": etag})

    @router.get(
        PREFIX + "/{artifact_id}/content",
        operation_id="downloadArtifactContent",
        summary="Download committed artifact content",
        openapi_extra={"parameters": _CONTENT_PARAMETERS},
        responses={
            200: {"description": "Full content."},
            206: {"description": "Partial content."},
            401: {"description": "Shared service credential required or invalid."},
            404: {"description": "Resource not found or not visible to the requested workspace."},
            410: {"description": "Content or event history has expired and is unavailable."},
        },
    )
    async def download_artifact_content(request: Request) -> Response:
        request_id = _request_id(request)
        service = get_service()
        if service is None:
            return artifact_error("artifact_service_unavailable", request_id)
        try:
            artifact_id = _artifact_id(request)
        except ValueError:
            return artifact_error("artifact_not_found", request_id)
        ranges = request.headers.getlist("range")
        if len(ranges) > 1:
            ranges = ["multiple"]  # One stable 416 path; never echo client input.
        try:
            download = await run_in_threadpool(
                service.download, artifact_id, ranges[0] if ranges else None
            )
        except ArtifactError as error:
            if error.code == "artifact_range_not_satisfiable":
                try:
                    artifact = await run_in_threadpool(service.metadata, artifact_id)
                except ArtifactError as refreshed:
                    return artifact_error(refreshed.code, request_id)
                return artifact_error(
                    error.code,
                    request_id,
                    param="Range",
                    headers={"Content-Range": f"bytes */{artifact.bytes}"},
                )
            return artifact_error(error.code, request_id)
        if matches_if_none_match(request.headers.get("if-none-match"), download.etag):
            return Response(
                status_code=304,
                headers={**_CACHE_HEADERS, "ETag": download.etag},
            )
        content = service.content(download)
        try:
            first = await run_in_threadpool(next, content)
        except ArtifactError as error:
            content.close()
            return artifact_error(error.code, request_id)
        except BaseException:
            content.close()
            raise
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(download.length),
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{service.filename(download.artifact)}"',
            "ETag": download.etag,
            "X-Content-Type-Options": "nosniff",
        }
        if download.partial:
            end = download.offset + download.length - 1
            headers["Content-Range"] = f"bytes {download.offset}-{end}/{download.artifact.bytes}"
        return _ArtifactContentResponse(
            chain((first,), content),
            content.close,
            headers=headers,
            status_code=206 if download.partial else 200,
            media_type="application/octet-stream",
        )

    return router


__all__ = ["PREFIX", "create_artifact_router"]
