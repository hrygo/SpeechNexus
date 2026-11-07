"""Collection-level pronunciation-set listing and immutable draft creation.

D2: this slice creates immutable draft revisions only. ``publish: true`` is an
explicitly unsupported intent and answers 422; omitting the field or sending
``false`` records a draft and never claims publication. The collection lives in
its own generated HTTP surface because ``pronunciations.py`` derives its single
contract path from ``next(iter(paths))`` and is the fixed-revision read surface.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.pronunciation_sets_http import (
    contract,
    validate_request,
    validate_response,
)
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from speechnexus_backend.speech.application.pronunciation_drafts import (
    PronunciationSets,
    PronunciationWriter,
)
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.pronunciation_drafts import (
    PronunciationDraftResult,
    PronunciationSetPage,
    PronunciationUnavailableError,
)
from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract
from speechnexus_backend.speech.interfaces.http.jobs import (
    _constant,
    _idempotency_key,
    _object,
    _receive,
)
from speechnexus_backend.speech.interfaces.pronunciation_drafts import (
    draft_to_wire,
    prepare_pronunciation_draft,
    summary_to_wire,
)

PATH = "/v1/nexus/pronunciation-sets"
LIST_OPERATION = "listPronunciationSets"
CREATE_OPERATION = "createPronunciationRevision"
_DEFAULT_LIMIT = 50
_MAX_CURSOR = 512

# (status, public type, message, wire code, retryable). ``retryable`` is a
# decision, not a function of the status: an unknown submission may only be
# retried unchanged when its original Idempotency-Key makes replay safe.
_ERRORS: dict[str, tuple[int, str, str, str, bool]] = {
    "invalid_request": (
        400,
        "invalid_request_error",
        "Invalid pronunciation request.",
        "invalid_request",
        False,
    ),
    "host_rejected": (
        403,
        "permission_error",
        "The pronunciation Host is not allowed.",
        "host_rejected",
        False,
    ),
    "origin_rejected": (
        403,
        "permission_error",
        "The pronunciation Origin is not allowed.",
        "origin_rejected",
        False,
    ),
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "The Idempotency-Key identifies another intent.",
        "idempotency_conflict",
        False,
    ),
    "configuration_changed": (
        409,
        "invalid_request_error",
        "The expected revision is not the current allocation head.",
        "configuration_changed",
        False,
    ),
    # A request without a key that may already have committed cannot be retried
    # safely: a resubmission is a new intent and may create another set. 409 is
    # the reconcile-the-original class, deliberately not a retryable 503.
    "outcome_unknown": (
        409,
        "invalid_request_error",
        "The original submission may have been accepted; resubmit only to create a new set.",
        "outcome_unknown",
        False,
    ),
    "publication_not_supported": (
        422,
        "invalid_request_error",
        "This operation creates immutable drafts; publication has its own command.",
        "publication_not_supported",
        False,
    ),
    # Only a request that carried its original Idempotency-Key reaches the
    # retryable form: replaying that key recovers the committed draft instead of
    # allocating a second one, so retrying is safe and bounded.
    "unavailable": (
        503,
        "service_unavailable_error",
        "Pronunciation draft storage is unavailable.",
        "unavailable",
        True,
    ),
    # Our own reply or the port's result violated the declared contract: an
    # implementation defect, not an outage, so it must not be dressed up as a
    # retryable 503.
    "contract_violation": (
        500,
        "api_error",
        "The pronunciation response violated the declared contract.",
        "contract_violation",
        False,
    ),
}


def pronunciation_sets_http_contract() -> dict[str, Any]:
    return namespaced_contract(contract(), "PronunciationSets_")


def pronunciation_sets_error(code: str, request_id: str, operation: str) -> JSONResponse:
    status, kind, message, wire, retryable = _ERRORS[code]
    headers = {"Cache-Control": "no-store", "X-Should-Retry": str(retryable).lower()}
    body = {
        "error": {
            "code": "pronunciation_" + wire,
            "type": kind,
            "message": message,
            "param": None,
            "request_id": request_id,
            "retryable": retryable,
        }
    }
    validate_response(operation, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


def cursor_for(after_id: str) -> str:
    """A canonical, unpadded base64 form of one family id."""
    return (
        base64.urlsafe_b64encode(identifier(after_id).encode("ascii")).decode("ascii").rstrip("=")
    )


def cursor_after(value: str) -> str:
    """Reject a cursor whose decoded id does not re-encode to the same bytes.

    The round-trip check is what makes tampering visible: a lax decoder accepts
    extra padding or the standard ``+/`` alphabet, and either silently decodes to
    a shorter, different id that would move the page. Only the canonical form is
    a cursor this deployment issued.
    """
    try:
        if not 1 <= len(value) <= _MAX_CURSOR:
            raise ValueError
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        after = identifier(raw.decode("ascii"))
        if cursor_for(after) != value:
            raise ValueError
        return after
    except ValueError, TypeError:
        raise ValueError("Invalid pronunciation cursor") from None


def _page_body(page: PronunciationSetPage) -> dict[str, Any]:
    more = page.has_more
    return {
        "object": "nexus.list",
        "data": [summary_to_wire(summary) for summary in page.summaries],
        "has_more": more,
        "next_cursor": cursor_for(page.summaries[-1].pronunciation_id) if more else None,
    }


def _list(
    reader: PronunciationSets, limit: int, after: str | None, request_id: str
) -> JSONResponse:
    try:
        body = _page_body(reader.list_sets(limit=limit, after=after))
        validate_response(LIST_OPERATION, 200, {"body": body})
    except PronunciationUnavailableError:
        # The port declares the read unavailable, including a history it cannot
        # decode; that declaration, not a connection or timeout detail, is the
        # 503 criterion.
        return pronunciation_sets_error("unavailable", request_id, LIST_OPERATION)
    except Exception:
        # A corrupt page or an unserializable summary is our defect. Mapping it
        # to a retryable outage would invite retries that can never succeed.
        return pronunciation_sets_error("contract_violation", request_id, LIST_OPERATION)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


# The port's result is a closed set. ``draft`` and ``error_code`` are part of the
# contract too: a created/replayed result carries a draft and no error, a
# conflict/rejected result carries the matching error and no draft, and an
# unknown result carries neither. Anything else is a port bug, not an outage.
_RESULT_ERRORS = {"conflict": "idempotency_conflict", "rejected": "configuration_changed"}


def _result_violation(result: PronunciationDraftResult) -> bool:
    if result.outcome in ("created", "replayed"):
        return result.draft is None or result.error_code is not None
    if result.outcome in _RESULT_ERRORS:
        return result.draft is not None or result.error_code != _RESULT_ERRORS[result.outcome]
    if result.outcome == "unknown":
        return result.draft is not None or result.error_code is not None
    return True


def _create(
    writer: PronunciationWriter,
    payload: dict[str, Any],
    key: str | None,
    request_id: str,
) -> JSONResponse:
    try:
        if payload.get("publish") is True:
            return pronunciation_sets_error(
                "publication_not_supported", request_id, CREATE_OPERATION
            )
        # Omission carries the declared draft default: the canonical intent is
        # always explicit so a keyless replay cannot differ by an injected field.
        intent = {**payload, "publish": False} if "publish" not in payload else payload
        command = prepare_pronunciation_draft(intent, key)
    except ContractValidationError:
        return pronunciation_sets_error("invalid_request", request_id, CREATE_OPERATION)
    except Exception:
        return pronunciation_sets_error("contract_violation", request_id, CREATE_OPERATION)
    try:
        result = writer.create(command)
    except PronunciationUnavailableError:
        # The port declares the storage unavailable (including a history it
        # cannot decode). This is its only declared failure, so it is the only
        # exception that may become the retryable 503.
        return pronunciation_sets_error("unavailable", request_id, CREATE_OPERATION)
    except Exception:
        return pronunciation_sets_error("contract_violation", request_id, CREATE_OPERATION)
    if _result_violation(result):
        return pronunciation_sets_error("contract_violation", request_id, CREATE_OPERATION)
    if result.outcome in ("created", "replayed"):
        draft = result.draft
        assert draft is not None  # narrowed by _result_violation
        try:
            body = draft_to_wire(draft)
            validate_response(CREATE_OPERATION, 201, {"body": body})
        except Exception:
            return pronunciation_sets_error("contract_violation", request_id, CREATE_OPERATION)
        return JSONResponse(body, status_code=201, headers={"Cache-Control": "no-store"})
    if result.outcome == "conflict":
        return pronunciation_sets_error("idempotency_conflict", request_id, CREATE_OPERATION)
    if result.outcome == "rejected":
        return pronunciation_sets_error("configuration_changed", request_id, CREATE_OPERATION)
    # Only "unknown" remains; _result_violation rejected every other outcome.
    if key is None:
        return pronunciation_sets_error("outcome_unknown", request_id, CREATE_OPERATION)
    return pronunciation_sets_error("unavailable", request_id, CREATE_OPERATION)


def create_pronunciation_sets_router(
    get_reader: Callable[[], PronunciationSets | None],
    get_writer: Callable[[], PronunciationWriter | None],
    *,
    max_bytes: int,
    receive_timeout_seconds: float,
) -> APIRouter:
    router = APIRouter(tags=["pronunciations"])

    @router.get(PATH, operation_id=LIST_OPERATION)
    async def list_sets(request: Request) -> JSONResponse:
        request_id = str(request.state.request_id)
        try:
            lengths = request.headers.getlist("content-length")
            if (lengths and lengths != ["0"]) or request.headers.getlist("transfer-encoding"):
                raise ValueError("A collection read carries no body")
            pairs = list(request.query_params.multi_items())
            if len(pairs) != len({key for key, _ in pairs}):
                raise ValueError("Duplicate query parameter")
            ids = request.headers.getlist("x-request-id")
            if len(ids) > 1:
                raise ValueError("Ambiguous request id")
            query = dict(pairs)
            validate_request(
                LIST_OPERATION,
                {"query": query, "headers": {"x-request-id": ids[0]} if ids else {}},
            )
            limit = int(query.get("limit", _DEFAULT_LIMIT))
            after = cursor_after(query["cursor"]) if "cursor" in query else None
        except ContractValidationError, ValueError, TypeError:
            return pronunciation_sets_error("invalid_request", request_id, LIST_OPERATION)
        reader = get_reader()
        if reader is None:
            return pronunciation_sets_error("unavailable", request_id, LIST_OPERATION)
        return await run_in_threadpool(_list, reader, limit, after, request_id)

    @router.post(PATH, operation_id=CREATE_OPERATION, status_code=201)
    async def create(request: Request) -> JSONResponse:
        request_id = str(request.state.request_id)
        try:
            if list(request.query_params.multi_items()):
                raise ValueError("A collection create carries no query")
            key = _idempotency_key(request)
            raw = await _receive(request, max_bytes, receive_timeout_seconds)
            payload = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
            if not isinstance(payload, dict):
                raise ValueError("Pronunciation creation requires an object")
            ids = request.headers.getlist("x-request-id")
            if len(ids) > 1:
                raise ValueError("Ambiguous request id")
            headers = {"x-request-id": ids[0]} if ids else {}
            if key is not None:
                headers["idempotency-key"] = key
            validate_request(CREATE_OPERATION, {"headers": headers, "body": payload})
        except ContractValidationError, ValueError, TypeError, RecursionError:
            return pronunciation_sets_error("invalid_request", request_id, CREATE_OPERATION)
        writer = get_writer()
        if writer is None:
            return pronunciation_sets_error("unavailable", request_id, CREATE_OPERATION)
        return await run_in_threadpool(_create, writer, payload, key, request_id)

    return router
