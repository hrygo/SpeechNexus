"""Only implemented Asset HTTP operations; no storage SDKs or object identities."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from itertools import chain
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError, parse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from speechnexus_backend.speech.application.assets import AssetService
from speechnexus_backend.speech.domain.assets import AssetError, UploadResult
from speechnexus_backend.speech.interfaces.http.asset_contract import (
    public_asset,
    validate_asset_parameter,
)
from speechnexus_backend.speech.interfaces.http.asset_errors import asset_error

_PREFIX = "/v1/nexus/assets"


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _location(request: Request, result: UploadResult) -> dict[str, str]:
    if result.asset is None:
        return {}
    return {"Location": request.scope.get("root_path", "") + _PREFIX + "/" + result.asset.asset_id}


class _AssetContentResponse(StreamingResponse):
    def __init__(self, content: Iterable[bytes], close: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._close = close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Includes client disconnect, failed sends and never-entered body
            # iterators: a prefetched version-pinned reader has one owner.
            await run_in_threadpool(self._close)


def create_asset_router(get_service: Callable[[], AssetService | None]) -> APIRouter:
    router = APIRouter(tags=["assets"])

    @router.post(_PREFIX, operation_id="uploadAsset")
    async def upload_asset(request: Request) -> Response:
        request_id = _request_id(request)
        service = get_service()
        if service is None:
            return asset_error("asset_service_unavailable", request_id)
        keys = request.headers.getlist("idempotency-key")
        try:
            if len(keys) > 1:
                raise ContractValidationError("Duplicate Idempotency-Key")
            if keys:
                validate_asset_parameter("IdempotencyKey", keys[0])
        except ContractValidationError:
            return asset_error("invalid_idempotency_key", request_id, param="Idempotency-Key")
        if (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
            != "multipart/form-data"
        ):
            return asset_error("invalid_asset_request", request_id)
        try:
            async with request.form(max_files=1, max_fields=4, max_part_size=512) as form:
                items = list(form.multi_items())
                names = [name for name, _ in items]
                if len(names) != len(set(names)) or set(names) - {
                    "file",
                    "purpose",
                    "workspace_id",
                    "ttl_seconds",
                    "sha256",
                }:
                    return asset_error("invalid_asset_request", request_id)
                upload = form.get("file")
                if not isinstance(upload, UploadFile):
                    return asset_error("invalid_asset_request", request_id, param="file")
                fields: dict[str, Any] = {"file": ""}  # Binary lives in the bounded private file.
                for name, value in items:
                    if name == "file":
                        continue
                    if not isinstance(value, str):
                        return asset_error("invalid_asset_request", request_id)
                    if name == "ttl_seconds":
                        if not re.fullmatch(r"[0-9]{1,8}", value):
                            return asset_error("invalid_asset_request", request_id, param=name)
                        fields[name] = int(value)
                    else:
                        fields[name] = value
                try:
                    command = parse("AssetUploadRequest", fields)
                except ContractValidationError:
                    return asset_error("invalid_asset_request", request_id)
                await upload.seek(0)
                result = await run_in_threadpool(
                    service.upload,
                    upload.file,
                    workspace_id=command.get("workspace_id", "workspace-default"),
                    purpose=command["purpose"],
                    ttl_seconds=command.get("ttl_seconds"),
                    expected_sha256=command.get("sha256"),
                    idempotency_key=keys[0] if keys else None,
                )
                location = _location(request, result)
                if result.outcome in ("unknown", "conflict", "in_progress", "rejected"):
                    fallback = (
                        "operation_in_progress"
                        if result.outcome == "in_progress"
                        else "asset_upload_unknown"
                    )
                    return asset_error(result.error_code or fallback, request_id, headers=location)
                if result.asset is None:
                    return asset_error("asset_service_unavailable", request_id)
                # Check current database expiry even for an old same-key replay;
                # a stored ready state alone is not permission to expose content.
                asset = await run_in_threadpool(service.metadata, result.asset.asset_id)
                return JSONResponse(
                    public_asset(asset),
                    status_code=201,
                    headers={"Cache-Control": "no-store", **location},
                )
        except HTTPException as error:
            # Starlette closes all parser-created files on multipart errors.
            if error.status_code == 400:
                return asset_error("invalid_asset_request", request_id)
            raise
        except AssetError as error:
            return asset_error(error.code, request_id)
        except OSError:
            return asset_error("asset_service_unavailable", request_id)

    @router.get(_PREFIX + "/{asset_id}", operation_id="getAsset")
    async def get_asset(request: Request, asset_id: str) -> Response:
        request_id = _request_id(request)
        service = get_service()
        if service is None:
            return asset_error("asset_service_unavailable", request_id)
        try:
            validate_asset_parameter("AssetIdPath", asset_id)
        except ContractValidationError:
            return asset_error("asset_not_found", request_id)
        try:
            asset = await run_in_threadpool(service.metadata, asset_id)
        except AssetError as error:
            return asset_error(error.code, request_id)
        return JSONResponse(public_asset(asset), headers={"Cache-Control": "no-store"})

    @router.get(_PREFIX + "/{asset_id}/content", operation_id="downloadAssetContent")
    async def download_content(request: Request, asset_id: str) -> Response:
        request_id = _request_id(request)
        service = get_service()
        if service is None:
            return asset_error("asset_service_unavailable", request_id)
        try:
            validate_asset_parameter("AssetIdPath", asset_id)
        except ContractValidationError:
            return asset_error("asset_not_found", request_id)
        ranges = request.headers.getlist("range")
        if len(ranges) > 1:
            ranges = ["multiple"]  # The same stable 416 path; never echo the value.
        try:
            download = await run_in_threadpool(
                service.download, asset_id, ranges[0] if ranges else None
            )
        except AssetError as error:
            if error.code == "range_not_satisfiable":
                try:
                    asset = await run_in_threadpool(service.metadata, asset_id)
                except AssetError as refreshed:
                    return asset_error(refreshed.code, request_id)
                return asset_error(
                    error.code,
                    request_id,
                    param="Range",
                    headers={"Content-Range": f"bytes */{asset.bytes}"},
                )
            return asset_error(error.code, request_id)
        content = service.content(download)
        try:
            first = await run_in_threadpool(next, content)
        except AssetError as error:
            content.close()
            return asset_error(error.code, request_id)
        except BaseException:
            content.close()
            raise
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(download.length),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{asset_id}.bin"',
        }
        if download.partial:
            end = download.offset + download.length - 1
            headers["Content-Range"] = f"bytes {download.offset}-{end}/{download.asset.bytes}"
        return _AssetContentResponse(
            chain((first,), content),
            content.close,
            headers=headers,
            status_code=206 if download.partial else 200,
            media_type="application/octet-stream",
        )

    return router
