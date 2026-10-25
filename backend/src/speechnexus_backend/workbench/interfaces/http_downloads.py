"""Generated public download operations over fully verified committed bytes."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from speechnexus_backend.workbench.application.downloads import (
    DownloadError,
    DownloadKind,
    PreparedDownload,
    WorkbenchDownloads,
)
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    read_envelope,
    request_id,
)
from speechnexus_backend.workbench.interfaces.http_versions import _error


class _DownloadResponse(StreamingResponse):
    def __init__(self, prepared: PreparedDownload, rid: str, *, timeout: float) -> None:
        self._prepared, self._timeout = prepared, timeout
        headers = {
            "X-Request-ID": rid,
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
            "Content-Length": str(prepared.length),
            "ETag": '"' + prepared.content.sha256 + '"',
        }
        if prepared.status == 206:
            end = prepared.start + prepared.length - 1
            headers["Content-Range"] = f"bytes {prepared.start}-{end}/{prepared.content.size}"
        super().__init__(
            self._chunks(),
            status_code=prepared.status,
            headers=headers,
            media_type="application/octet-stream",
        )

    async def _chunks(self) -> AsyncIterator[bytes]:
        offset, remaining = self._prepared.start, self._prepared.length
        while remaining:
            chunk = await self._prepared.body.read(offset, min(64 * 1024, remaining))
            if not chunk or len(chunk) > remaining:
                raise OSError("verified download buffer unavailable")
            yield chunk
            offset, remaining = offset + len(chunk), remaining - len(chunk)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Covers disconnect before the generator starts, send failure, and timeout.
        # A generator-only finally/background callback can leak the prepared spool.
        try:
            async with asyncio.timeout(self._timeout):
                await super().__call__(scope, receive, send)
        finally:
            await self._prepared.close()


def create_download_router(
    service: WorkbenchDownloads | None,
    *,
    default_workspace_id: str,
    max_json_bytes: int,
    body_timeout_seconds: float,
    download_timeout_seconds: float,
) -> APIRouter:
    router = APIRouter()

    def endpoint(
        operation: str, kind: DownloadKind, field: str
    ) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            rid = request_id(request)
            prepared: PreparedDownload | None = None
            response: Response
            try:
                value = await read_envelope(
                    request,
                    operation,
                    has_body=False,
                    max_json_bytes=max_json_bytes,
                    body_timeout_seconds=body_timeout_seconds,
                    extra_headers=("range",),
                )
                if service is None:
                    raise DownloadError("download_unavailable")
                prepared = await service.prepare(
                    kind=kind,
                    ident=value["path"][field],
                    workspace_id=default_workspace_id,
                    range_header=value["headers"].get("range"),
                )
                response = _DownloadResponse(prepared, rid, timeout=download_timeout_seconds)
                # Generated validation checks metadata without buffering/copying binary bytes.
                validate_response(
                    operation, prepared.status, {"body": b"", "headers": dict(response.headers)}
                )
                return response
            except IngressError as error:
                return _error(error.code, rid, operation)
            except DownloadError as error:
                return _error(
                    error.code,
                    rid,
                    operation,
                    content_range=None if error.size is None else f"bytes */{error.size}",
                )
            except ResourceNotFoundError:
                return _error("resource_not_found", rid, operation)
            except VersionStoreError:
                return _error("store_unavailable", rid, operation)
            except Exception:
                if prepared is not None:
                    await prepared.close()
                return _error("internal_error", rid, operation)

        return handle

    operations: dict[str, tuple[DownloadKind, str]] = {
        "downloadSavedAsset": ("saved", "asset_id"),
        "downloadBookExport": ("export", "export_id"),
    }
    for path, item in contract()["paths"].items():
        for method, operation in item.items():
            name = operation["operationId"]
            if name in operations:
                router.add_api_route(
                    path,
                    endpoint(name, *operations[name]),
                    methods=[method.upper()],
                    operation_id=name,
                    response_model=None,
                )
    return router
