"""The frozen voice catalogue reads and the revision retirement command."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError, parse
from speechnexus_contracts.voice_catalogue_http import (
    contract,
    validate_request,
    validate_response,
)
from speechnexus_contracts.voice_retirement_http import (
    contract as retirement_contract,
)
from speechnexus_contracts.voice_retirement_http import (
    validate_request as validate_retirement_request,
)
from speechnexus_contracts.voice_retirement_http import (
    validate_response as validate_retirement_response,
)
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.voice_catalogue import VoiceCatalogue
from speechnexus_backend.speech.application.voice_retirements import VoiceRetirements
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.domain.voice_lifecycle import (
    VoiceRetirementCommand,
    VoiceRetirementConflictError,
    VoiceRetirementUnavailableError,
    VoiceRevisionNotFoundError,
)
from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract

RETIREMENT_OPERATION = "retireVoiceRevision"
_MAX_BODY_BYTES = 65_536

_ERRORS: dict[str, tuple[int, str, str]] = {
    "voice_invalid_request": (400, "invalid_request_error", "The voice request is invalid."),
    "voice_not_found": (404, "not_found_error", "The voice revision was not found."),
    "voice_retirement_conflict": (
        409,
        "invalid_request_error",
        "Only the selected voice revision can be retired with this intent.",
    ),
    "voice_disable_unsupported": (
        422,
        "invalid_request_error",
        "This deployment cannot cancel in-flight use, so disable is unsupported.",
    ),
    "voice_catalogue_unavailable": (
        503,
        "service_unavailable_error",
        "The voice catalogue is unavailable.",
    ),
    "voice_retirement_unavailable": (
        503,
        "service_unavailable_error",
        "The voice retirement result is unknown.",
    ),
}


def voice_http_contract() -> dict[str, Any]:
    return namespaced_contract(contract(), "VoiceCatalogue_")


def voice_retirement_http_contract() -> dict[str, Any]:
    return namespaced_contract(retirement_contract(), "VoiceRetirement_")


def _json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_: str) -> Any:
        raise ValueError("Nonfinite JSON number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def _validated(operation: str, status: int, envelope: dict[str, Any]) -> None:
    if operation == RETIREMENT_OPERATION:
        validate_retirement_response(operation, status, envelope)
    else:
        validate_response(operation, status, envelope)


def _error(code: str, request_id: str, operation: str) -> JSONResponse:
    status, kind, message = _ERRORS[code]
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
    _validated(operation, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


def _retire(
    catalogue: VoiceCatalogue,
    retirements: VoiceRetirements,
    command: VoiceRetirementCommand,
    request_id: str,
) -> Response:
    try:
        receipt = retirements.retire(command)
        body = catalogue.get_revision(command.voice_id, command.revision)
        if body is None:
            return _error("voice_retirement_unavailable", request_id, RETIREMENT_OPERATION)
        if (body["voice_id"], body["revision"]) != (receipt.voice_id, receipt.revision):
            return _error("voice_retirement_unavailable", request_id, RETIREMENT_OPERATION)
        expected = "disabled" if receipt.mode == "disable" else "retired"
        if body["status"] != expected:
            return _error("voice_retirement_unavailable", request_id, RETIREMENT_OPERATION)
        validate_retirement_response(RETIREMENT_OPERATION, 200, {"body": body})
        return JSONResponse(body, headers={"Cache-Control": "no-store"})
    except VoiceRevisionNotFoundError:
        return _error("voice_not_found", request_id, RETIREMENT_OPERATION)
    except VoiceRetirementConflictError:
        return _error("voice_retirement_conflict", request_id, RETIREMENT_OPERATION)
    except (
        VoiceRetirementUnavailableError,
        PublicationUnavailableError,
        ContractValidationError,
        ValueError,
        TypeError,
        KeyError,
    ):
        return _error("voice_retirement_unavailable", request_id, RETIREMENT_OPERATION)


def create_voice_router(
    get_catalogue: Callable[[], VoiceCatalogue | None],
    get_retirements: Callable[[], VoiceRetirements | None] = lambda: None,
) -> APIRouter:
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
            return _error("voice_invalid_request", request_id, operation)

        provider = get_catalogue()
        if provider is None:
            return _error("voice_catalogue_unavailable", request_id, operation)
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
                    return _error("voice_not_found", request_id, operation)
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
            return _error("voice_catalogue_unavailable", request_id, operation)

    @router.get("/v1/nexus/voices", operation_id="listVoices")
    async def list_voices(request: Request) -> JSONResponse:
        return await run_in_threadpool(read, request, "listVoices")

    @router.get("/v1/nexus/voices/{voice_id}/revisions/{revision}", operation_id="getVoiceRevision")
    async def get_revision(request: Request) -> JSONResponse:
        return await run_in_threadpool(read, request, "getVoiceRevision")

    @router.post(
        "/v1/nexus/voices/{voice_id}/revisions/{revision}/retirements",
        operation_id=RETIREMENT_OPERATION,
    )
    async def retire(request: Request) -> Response:
        request_id = str(request.state.request_id)
        try:
            if list(request.query_params.multi_items()):
                raise ContractValidationError("Retirement commands have no query")
            headers: dict[str, str] = {}
            for name in ("x-request-id", "idempotency-key"):
                values = request.headers.getlist(name)
                if len(values) > 1:
                    raise ContractValidationError("Ambiguous header")
                if values:
                    headers[name] = values[0]
            raw = await request.body()
            if len(raw) > _MAX_BODY_BYTES:
                raise ContractValidationError("Retirement body is too large")
            body = _json(raw)
            envelope = {"path": request.path_params, "headers": headers, "body": body}
            validate_retirement_request(RETIREMENT_OPERATION, envelope)
        except ContractValidationError, ValueError, TypeError, KeyError, RecursionError:
            return _error("voice_invalid_request", request_id, RETIREMENT_OPERATION)
        if body["mode"] == "disable":
            # No durable voice-disable intent or target capture exists yet, so
            # claiming a cancellation would report an unproven stop as success.
            return _error("voice_disable_unsupported", request_id, RETIREMENT_OPERATION)
        catalogue, retirements = get_catalogue(), get_retirements()
        if catalogue is None or retirements is None:
            return _error("voice_catalogue_unavailable", request_id, RETIREMENT_OPERATION)
        try:
            command = VoiceRetirementCommand(
                voice_id=request.path_params["voice_id"],
                revision=request.path_params["revision"],
                mode=body["mode"],
                reason=body.get("reason"),
                request_id=request_id,
            )
        except ValueError:
            return _error("voice_invalid_request", request_id, RETIREMENT_OPERATION)
        return await run_in_threadpool(_retire, catalogue, retirements, command, request_id)

    return router
