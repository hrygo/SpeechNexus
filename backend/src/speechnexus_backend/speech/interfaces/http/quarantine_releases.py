"""Operator-verified quarantine release HTTP boundary; no private fact is echoed.

The route validates the contract before touching storage, converts the evidence
into domain form, and maps one stable decision per exception. It never decides
capacity itself and never invents a stop conclusion.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin_http import validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.quarantine_releases import (
    QuarantineReleaseService,
    QuarantineUnavailableError,
    QuarantineUnknownError,
)
from speechnexus_backend.speech.domain.quarantine_release import (
    QuarantineReleaseRejectedError,
    ReleaseEvidence,
    ReleaseOutcome,
    prepare_evidence,
)
from speechnexus_backend.speech.interfaces.http.quarantine_release_errors import (
    OPERATION,
    release_error,
)

PATH = "/v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases"


class _TooLargeError(Exception):
    """The declared or streamed body exceeds the route budget."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def _stamp(value_us: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value_us)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _idempotency_key(request: Request) -> str | None:
    values = request.headers.getlist("idempotency-key")
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("Duplicate Idempotency-Key")
    key = values[0]
    if not 8 <= len(key) <= 128 or not key.isascii() or any(ord(c) < 32 for c in key):
        raise ValueError("Invalid Idempotency-Key")
    return key


async def _body(request: Request, max_bytes: int, receive_timeout: float) -> bytes:
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise ValueError("Duplicate Content-Length")
    if lengths:
        try:
            declared = int(lengths[0])
        except ValueError:
            raise ValueError("Invalid Content-Length") from None
        if declared < 0 or declared > max_bytes:
            raise _TooLargeError
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("Quarantine release requires JSON")
    chunks: list[bytes] = []
    size = 0
    try:
        async with asyncio.timeout(receive_timeout):
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise _TooLargeError
                chunks.append(chunk)
    except TimeoutError:
        raise ValueError("Quarantine release receive timeout") from None
    except ClientDisconnect:
        raise ValueError("Quarantine release client disconnected") from None
    return b"".join(chunks)


def _wire(outcome: ReleaseOutcome) -> dict[str, Any]:
    ledger = outcome.ledger
    return {
        "lease_id": ledger.lease_id,
        "object": "nexus.quarantine_release",
        "lease_state": "released",
        "cleanup_state": "complete",
        "budget_released_at": _stamp(ledger.budget_released_at_us),
        "evidence_kind": ledger.evidence_kind,
        "evidence_digest": ledger.evidence_digest,
        "evidence_observed_at": _stamp(ledger.evidence_observed_at_us),
        "reason": ledger.reason,
        "idempotent_replay": outcome.idempotent_replay,
        "released_submit_intent_ids": list(ledger.released_submit_intent_ids),
    }


def _reject(error: QuarantineReleaseRejectedError, request_id: str) -> Response:
    mapping = {
        "invalid": "quarantine_release_invalid",
        "not_found": "quarantine_release_not_found",
        "idempotency_conflict": "idempotency_conflict",
        "quarantine_release_conflict": "quarantine_release_conflict",
    }
    return release_error(mapping.get(error.code, "quarantine_release_unknown"), request_id)


def _release(
    service: QuarantineReleaseService,
    evidence: ReleaseEvidence,
    request_id: str,
) -> Response:
    try:
        outcome = service.release(evidence)
        body = _wire(outcome)
        validate_response(OPERATION, 200, {"body": body})
        return JSONResponse(
            body, status_code=200, headers={"Cache-Control": "no-store", "X-Request-ID": request_id}
        )
    except QuarantineReleaseRejectedError as error:
        return _reject(error, request_id)
    except QuarantineUnavailableError:
        return release_error("quarantine_release_unavailable", request_id)
    except QuarantineUnknownError:
        # A lost commit acknowledgement is a query-first result, never a retry hint.
        return release_error("quarantine_release_unknown", request_id)
    except Exception:
        return release_error("quarantine_release_unknown", request_id)


def create_quarantine_release_router(
    get_service: Callable[[], QuarantineReleaseService | None],
    *,
    max_bytes: int = 16384,
    receive_timeout_seconds: float = 5,
) -> APIRouter:
    if not 1 <= max_bytes <= 1048576:
        raise ValueError("Quarantine release body must be bounded")
    if not 0 < receive_timeout_seconds <= 30:
        raise ValueError("Quarantine release receive deadline must be bounded")
    router = APIRouter(tags=["admin"])

    async def release(request: Request) -> Response:
        request_id = str(request.state.request_id)
        service = get_service()
        if service is None:
            return release_error("quarantine_release_unavailable", request_id)
        try:
            key = _idempotency_key(request)
        except ValueError:
            return release_error("idempotency_key_invalid", request_id)
        try:
            raw = await _body(request, max_bytes, receive_timeout_seconds)
            body = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
            )
            if not isinstance(body, dict):
                raise ValueError("Quarantine release body must be an object")
            headers = {}
            for name in ("x-request-id", "idempotency-key"):
                values = request.headers.getlist(name)
                if len(values) > 1:
                    raise ValueError("Ambiguous header")
                if values:
                    headers[name] = values[0]
            envelope: dict[str, Any] = {
                "path": dict(request.path_params),
                "query": {},
                "headers": headers,
                "body": body,
            }
            if request.query_params:
                raise ValueError("Quarantine release takes no query parameters")
            validate_request(OPERATION, envelope)
        except ContractValidationError, ValueError, TypeError, RecursionError, _TooLargeError:
            return release_error("quarantine_release_invalid", request_id)
        try:
            evidence = prepare_evidence(
                lease_id=envelope["path"]["lease_id"],
                evidence_kind=body.get("evidence_kind"),
                evidence_digest=body.get("evidence_digest"),
                observed_at=body.get("observed_at"),
                reason=body.get("reason"),
                idempotency_key=key,
            )
        except ValueError:
            return release_error("quarantine_release_invalid", request_id)
        return await run_in_threadpool(_release, service, evidence, request_id)

    router.add_api_route(PATH, release, methods=["POST"], operation_id=OPERATION)
    return router


__all__ = ["PATH", "create_quarantine_release_router"]
