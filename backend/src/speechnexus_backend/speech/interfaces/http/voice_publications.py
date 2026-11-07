"""HTTP boundary for publishing one validated voice draft revision.

Kept separate from the other voice-draft operations because its declared statuses
and stable failure codes differ: a recorded eligibility decision is 422, while a
missing report, evidence or storage is a retryable 503.
"""

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError, parse
from speechnexus_contracts.voice_drafts_http import validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from speechnexus_backend.speech.application.voice_drafts import VoiceDraftConflictError
from speechnexus_backend.speech.application.voice_publications import (
    VoicePublicationConflictError,
    VoicePublicationContractError,
    VoicePublicationInputError,
    VoicePublicationNotEligibleError,
    VoicePublicationNotFoundError,
    VoicePublications,
    VoicePublicationUnavailableError,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.interfaces.http.jobs import (
    _constant,
    _idempotency_key,
    _object,
    _receive,
    _TooLargeError,
)
from speechnexus_backend.speech.interfaces.http.origin_policy import rejected_authority

_OPERATION = "publishVoiceDraftRevision"
_RECEIVE_LIMIT = 65536

# (status, public type, message, wire code, retryable). The wire codes are the
# operation's stable decisions: an eligibility rejection is not a conflict and a
# transient report/storage outage is not an implementation placeholder.
_ERRORS: dict[str, tuple[int, str, str, str, bool]] = {
    "invalid_request": (
        400,
        "invalid_request_error",
        "Invalid voice publication request.",
        "voice_draft_invalid_request",
        False,
    ),
    "authority_rejected": (
        403,
        "permission_error",
        "The voice Host or Origin is not allowed.",
        "voice_draft_authority_rejected",
        False,
    ),
    "not_found": (
        404,
        "not_found_error",
        "The voice draft was not found.",
        "voice_draft_not_found",
        False,
    ),
    "conflict": (
        409,
        "invalid_request_error",
        "The voice draft changed or the publication intent conflicts.",
        "voice_draft_conflict",
        False,
    ),
    "not_eligible": (
        422,
        "invalid_request_error",
        "The draft revision is not eligible for publication.",
        "voice_publication_not_eligible",
        False,
    ),
    "unavailable": (
        503,
        "service_unavailable_error",
        "Voice publication storage or the validation report is unavailable.",
        "voice_draft_unavailable",
        True,
    ),
    "contract_violation": (
        500,
        "api_error",
        "The voice publication response violated the declared contract.",
        "voice_draft_contract_violation",
        False,
    ),
}


def _error(code: str, request_id: str) -> JSONResponse:
    status, kind, message, wire, retryable = _ERRORS[code]
    headers = {"Cache-Control": "no-store", "X-Should-Retry": str(retryable).lower()}
    body = parse(
        "ErrorResponse",
        {
            "error": {
                "code": wire,
                "type": kind,
                "message": message,
                "param": None,
                "request_id": request_id,
                "retryable": retryable,
            }
        },
    )
    validate_response(_OPERATION, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


def _execute(
    provider: VoicePublications, request: Request, payload: dict[str, Any], key: str | None
) -> JSONResponse:
    request_id = str(request.state.request_id)
    try:
        body, created = provider.publish(
            request.path_params["draft_id"],
            payload,
            request_id=request_id,
            idempotency_key=key,
        )
        status = 201 if created else 200
        validate_response(_OPERATION, status, {"body": body})
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
    except VoicePublicationInputError:
        return _error("invalid_request", request_id)
    except VoicePublicationNotFoundError:
        return _error("not_found", request_id)
    except VoicePublicationConflictError, VoiceDraftConflictError:
        return _error("conflict", request_id)
    except VoicePublicationNotEligibleError:
        return _error("not_eligible", request_id)
    except VoicePublicationUnavailableError, PublicJobUnavailableError:
        return _error("unavailable", request_id)
    except ContractValidationError, VoicePublicationContractError:
        return _error("contract_violation", request_id)
    except ValueError, KeyError, TypeError, RecursionError:
        return _error("unavailable", request_id)


def create_voice_publications_router(
    get_provider: Callable[[], VoicePublications | None],
    *,
    allowed_hosts: tuple[str, ...],
    allowed_origins: tuple[str, ...],
) -> APIRouter:
    router = APIRouter(tags=["voice-creation"])

    async def publish(request: Request) -> JSONResponse:
        request_id = str(request.state.request_id)
        if rejected_authority(
            request.headers, frozenset(allowed_hosts), frozenset(allowed_origins)
        ):
            return _error("authority_rejected", request_id)
        payload: Any = {}
        try:
            key = _idempotency_key(request)
            pairs = list(request.query_params.multi_items())
            if len(pairs) != len({name for name, _ in pairs}):
                raise ValueError("Duplicate query parameter")
            headers: dict[str, str] = {}
            for name in ("x-request-id", "idempotency-key"):
                values = request.headers.getlist(name)
                if len(values) > 1:
                    raise ValueError("Duplicate header")
                if values:
                    headers[name] = values[0]
            raw = await _receive(request, _RECEIVE_LIMIT, 3)
            payload = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
            validate_request(
                _OPERATION,
                {
                    "path": request.path_params,
                    "query": dict(pairs),
                    "headers": headers,
                    "body": payload,
                },
            )
        except ContractValidationError, ValueError, TypeError, RecursionError, _TooLargeError:
            return _error("invalid_request", request_id)
        provider = get_provider()
        if provider is None:
            return _error("unavailable", request_id)
        return await run_in_threadpool(_execute, provider, request, payload, key)

    router.add_api_route(
        "/v1/nexus/voice-drafts/{draft_id}/publications",
        publish,
        methods=["POST"],
        operation_id=_OPERATION,
        status_code=201,
    )
    return router
