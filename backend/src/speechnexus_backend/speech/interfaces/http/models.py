"""Implemented model discovery over the published catalogue; nothing is invented."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.model_catalog import model_list
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.interfaces.http.model_errors import model_error

_PREFIX = "/v1/models"


class ModelCatalogue(Protocol):
    def list_models(self) -> list[dict[str, Any]]: ...


def create_model_router(get_catalogue: Callable[[], ModelCatalogue | None]) -> APIRouter:
    router = APIRouter(tags=["models"])

    @router.get(_PREFIX, operation_id="listModels")
    async def list_models(request: Request) -> Response:
        request_id = str(request.state.request_id)
        catalogue = get_catalogue()
        if catalogue is None:
            return model_error("model_catalogue_unavailable", request_id)
        try:
            models = await run_in_threadpool(catalogue.list_models)
        except PublicationUnavailableError:
            return model_error("model_catalogue_unavailable", request_id)
        return JSONResponse(model_list(models), headers={"Cache-Control": "no-store"})

    @router.get(_PREFIX + "/{model}", operation_id="retrieveModel")
    async def retrieve_model(request: Request, model: str) -> Response:
        request_id = str(request.state.request_id)
        catalogue = get_catalogue()
        if catalogue is None:
            return model_error("model_catalogue_unavailable", request_id)
        try:
            models = await run_in_threadpool(catalogue.list_models)
        except PublicationUnavailableError:
            return model_error("model_catalogue_unavailable", request_id)
        # The catalogue is small and published-only; filtering keeps one source of truth.
        for entry in models:
            if entry.get("id") == model:
                return JSONResponse(entry, headers={"Cache-Control": "no-store"})
        return model_error("model_not_found", request_id)

    return router
