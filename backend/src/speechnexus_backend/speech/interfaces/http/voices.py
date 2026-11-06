"""The frozen voice catalogue GETs over the existing published authority."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError, parse
from speechnexus_contracts.voice_catalogue_http import contract, validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from speechnexus_backend.speech.application.voice_catalogue import VoiceCatalogue
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract


def voice_http_contract() -> dict[str, Any]:
    return namespaced_contract(contract(), "VoiceCatalogue_")


def _error(operation: str, request_id: str, status: int = 503) -> JSONResponse:
    code, kind, message = {
        400: ("voice_invalid_request", "invalid_request_error", "The voice request is invalid."),
        404: ("voice_not_found", "not_found_error", "The voice revision was not found."),
        503: (
            "voice_catalogue_unavailable",
            "service_unavailable_error",
            "The voice catalogue is unavailable.",
        ),
    }[status]
    retryable = status == 503
    headers = {"Cache-Control": "no-store", "X-Should-Retry": str(retryable).lower()}
    body = parse(
        "ErrorResponse",
        {
            "error": {
                "code": code,
                "type": kind,
                "message": message,
                "param": None,
                "request_id": request_id,
                "retryable": retryable,
            }
        },
    )
    validate_response(operation, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


def create_voice_router(get_catalogue: Callable[[], VoiceCatalogue | None]) -> APIRouter:
    router = APIRouter(tags=["voices"])

    def read(request: Request, operation: str) -> JSONResponse:
        request_id = str(request.state.request_id)
        try:
            pairs = list(request.query_params.multi_items())
            if len(pairs) != len({key for key, _ in pairs}):
                raise ValueError("Duplicate query parameter")
            query = dict(pairs)
            validate_request(operation, {"path": request.path_params, "query": query})
        except ContractValidationError, ValueError:
            return _error(operation, request_id, 400)

        provider = get_catalogue()
        if provider is None:
            return _error(operation, request_id)
        try:
            body: dict[str, Any] | None
            if operation == "listVoices":
                body = provider.list_voices(
                    language=query.get("language"),
                    limit=int(query.get("limit", "50")),
                    cursor=query.get("cursor"),
                )
            else:
                body = provider.get_revision(
                    request.path_params["voice_id"], request.path_params["revision"]
                )
                if body is None:
                    return _error(operation, request_id, 404)
                if (body["voice_id"], body["revision"]) != (
                    request.path_params["voice_id"],
                    request.path_params["revision"],
                ):
                    raise ValueError("Wrong voice revision")
            validate_response(operation, 200, {"body": body})
            return JSONResponse(body, headers={"Cache-Control": "no-store"})
        except (
            PublicationUnavailableError,
            ContractValidationError,
            ValueError,
            TypeError,
            KeyError,
        ):
            return _error(operation, request_id)

    @router.get("/v1/nexus/voices", operation_id="listVoices")
    async def list_voices(request: Request) -> JSONResponse:
        return await run_in_threadpool(read, request, "listVoices")

    @router.get("/v1/nexus/voices/{voice_id}/revisions/{revision}", operation_id="getVoiceRevision")
    async def get_revision(request: Request) -> JSONResponse:
        return await run_in_threadpool(read, request, "getVoiceRevision")

    return router
