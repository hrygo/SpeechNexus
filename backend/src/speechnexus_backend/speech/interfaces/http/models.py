"""Implemented model discovery over the published catalogue; nothing is invented."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.capability_snapshot import (
    CapabilityPolicy,
    capability_snapshot,
    catalogue_etag,
    matches_if_none_match,
    snapshot_etag,
)
from speechnexus_backend.speech.application.model_catalog import (
    ModelCatalogueSnapshot,
    model_list,
)
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.interfaces.http.model_errors import model_error

_PREFIX = "/v1/models"
_CAPABILITIES = "/v1/nexus/capabilities"
_CACHE_HEADERS = {"Cache-Control": "no-store"}


class ModelCatalogue(Protocol):
    def snapshot(self) -> ModelCatalogueSnapshot: ...


def create_model_router(
    get_catalogue: Callable[[], ModelCatalogue | None], policy: CapabilityPolicy
) -> APIRouter:
    router = APIRouter(tags=["models"])

    @router.get(_PREFIX, operation_id="listModels")
    async def list_models(request: Request) -> Response:
        request_id = str(request.state.request_id)
        catalogue = get_catalogue()
        if catalogue is None:
            return model_error("model_catalogue_unavailable", request_id)
        try:
            snapshot = await run_in_threadpool(catalogue.snapshot)
            body = model_list(list(snapshot.models))
            etag = catalogue_etag(
                kind="models",
                policy=policy,
                workspace_id=policy.workspace_id,
                catalog_revision=snapshot.catalog_revision,
                payload=body,
            )
        except PublicationUnavailableError, TypeError, ValueError:
            return model_error("model_catalogue_unavailable", request_id)
        if matches_if_none_match(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers={**_CACHE_HEADERS, "ETag": etag})
        return JSONResponse(body, headers={**_CACHE_HEADERS, "ETag": etag})

    @router.get(_PREFIX + "/{model}", operation_id="retrieveModel")
    async def retrieve_model(request: Request, model: str) -> Response:
        request_id = str(request.state.request_id)
        catalogue = get_catalogue()
        if catalogue is None:
            return model_error("model_catalogue_unavailable", request_id)
        try:
            snapshot = await run_in_threadpool(catalogue.snapshot)
            # The catalogue is small and published-only; filtering keeps one source of truth.
            entry = next((item for item in snapshot.models if item.get("id") == model), None)
            if entry is None:
                return model_error("model_not_found", request_id)
            etag = catalogue_etag(
                kind="model",
                policy=policy,
                workspace_id=policy.workspace_id,
                catalog_revision=snapshot.catalog_revision,
                payload=entry,
            )
        except PublicationUnavailableError, TypeError, ValueError:
            return model_error("model_catalogue_unavailable", request_id)
        if matches_if_none_match(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers={**_CACHE_HEADERS, "ETag": etag})
        return JSONResponse(entry, headers={**_CACHE_HEADERS, "ETag": etag})

    @router.get(_CAPABILITIES, operation_id="getCapabilities", tags=["capabilities"])
    async def get_capabilities(request: Request, workspace_id: str | None = None) -> Response:
        request_id = str(request.state.request_id)
        selected_workspace = workspace_id or policy.workspace_id
        try:
            identifier(selected_workspace)
        except ValueError:
            return model_error("capability_workspace_not_found", request_id)
        catalogue = get_catalogue()
        if catalogue is None:
            return model_error("model_catalogue_unavailable", request_id)
        try:
            snapshot = await run_in_threadpool(catalogue.snapshot)
            body = capability_snapshot(
                models=snapshot.capability_models,
                profiles=snapshot.profiles,
                voices=snapshot.voices,
                catalog_revision=snapshot.catalog_revision,
                generated_at_us=snapshot.generated_at_us,
                policy=policy,
                workspace_id=selected_workspace,
            )
            etag = snapshot_etag(
                policy=policy,
                workspace_id=selected_workspace,
                catalog_revision=snapshot.catalog_revision,
                snapshot=body,
            )
        except PublicationUnavailableError, TypeError, ValueError:
            return model_error("model_catalogue_unavailable", request_id)
        if matches_if_none_match(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers={**_CACHE_HEADERS, "ETag": etag})
        return JSONResponse(body, headers={**_CACHE_HEADERS, "ETag": etag})

    return router
