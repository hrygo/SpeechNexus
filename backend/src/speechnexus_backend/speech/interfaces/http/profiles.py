"""Actual draft POST and pinned GET; uncertainty never becomes absence or a new key."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.profile_http import validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.profile_drafts import ProfileDrafts
from speechnexus_backend.speech.domain.profile_drafts import ProfileUnavailableError
from speechnexus_backend.speech.interfaces.http.profile_errors import CREATE, READ, profile_error
from speechnexus_backend.speech.interfaces.http.profile_ingress import CREATE_PATH, READ_PATH
from speechnexus_backend.speech.interfaces.profile_drafts import (
    draft_to_wire,
    prepare_profile_draft,
)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def _create(
    provider: ProfileDrafts, envelope: dict[str, Any], raw: bytes, request_id: str
) -> Response:
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        envelope = {**envelope, "body": body}
        validate_request(CREATE, envelope)
    except ValueError, TypeError, RecursionError:
        return profile_error("invalid_profile_request", request_id, operation=CREATE)
    try:
        command = prepare_profile_draft(body, envelope["headers"].get("idempotency-key"))
    except ContractValidationError:
        return profile_error("invalid_profile_definition", request_id, operation=CREATE)
    try:
        result = provider.create(command)
        if result.outcome in ("conflict", "rejected"):
            expected = (
                "idempotency_conflict" if result.outcome == "conflict" else "configuration_changed"
            )
            if result.error_code != expected:
                raise ProfileUnavailableError
            return profile_error(expected, request_id, operation=CREATE)
        if result.outcome not in ("created", "replayed") or result.draft is None:
            raise ProfileUnavailableError
        if (
            result.draft.profile_id != command.profile_id
            or result.draft.definition != command.definition
        ):
            raise ProfileUnavailableError
        wire = draft_to_wire(result.draft)
        validate_response(CREATE, 201, {"body": wire})
        return JSONResponse(wire, status_code=201, headers={"Cache-Control": "no-store"})
    except Exception:
        # No raw driver/validation diagnostics or caller data leave this boundary.
        return profile_error("profile_result_unknown", request_id, operation=CREATE)


def _read(provider: ProfileDrafts, envelope: dict[str, Any], request_id: str) -> Response:
    try:
        validate_request(READ, envelope)
    except ContractValidationError, ValueError, TypeError, RecursionError:
        return profile_error("invalid_profile_request", request_id, operation=READ)
    try:
        identity, revision = envelope["path"]["profile_id"], envelope["path"]["revision"]
        draft = provider.get(identity, revision)
        if draft is None:
            return profile_error("profile_not_found", request_id, operation=READ)
        if draft.profile_id != identity or draft.revision != revision:
            raise ProfileUnavailableError
        wire = draft_to_wire(draft)
        validate_response(READ, 200, {"body": wire})
        return JSONResponse(wire, headers={"Cache-Control": "no-store"})
    except Exception:
        return profile_error("profile_unavailable", request_id, operation=READ)


def create_profile_router(get_provider: Callable[[], ProfileDrafts | None]) -> APIRouter:
    router = APIRouter(tags=["profiles"])

    async def handle(request: Request, *, create: bool) -> Response:
        operation, request_id = (CREATE if create else READ), str(request.state.request_id)
        provider = get_provider()
        if provider is None:
            return profile_error(
                "profile_result_unknown" if create else "profile_unavailable",
                request_id,
                operation=operation,
            )
        try:
            pairs = list(request.query_params.multi_items())
            if len(pairs) != len({key for key, _ in pairs}):
                raise ContractValidationError("Ambiguous query")
            headers: dict[str, str] = {}
            for name in ("x-request-id", "idempotency-key"):
                values = request.headers.getlist(name)
                if len(values) > 1:
                    raise ContractValidationError("Ambiguous header")
                if values:
                    headers[name] = values[0]
            envelope = {"path": request.path_params, "query": dict(pairs), "headers": headers}
            if create:
                raw = await request.body()  # ProfileIngress has bounded the actual bytes and time.
                return await run_in_threadpool(_create, provider, envelope, raw, request_id)
            lengths = request.headers.getlist("content-length")
            if (
                len(lengths) > 1
                or (lengths and lengths != ["0"])
                or request.headers.getlist("transfer-encoding")
            ):
                raise ContractValidationError("A fixed profile read has no body")
            async with asyncio.timeout(1):
                for _ in range(8):
                    message = await request.receive()
                    if message["type"] != "http.request" or message.get("body"):
                        raise ContractValidationError("A fixed profile read has no body")
                    if not message.get("more_body", False):
                        break
                else:
                    raise ContractValidationError("Read receive budget exceeded")
            return await run_in_threadpool(_read, provider, envelope, request_id)
        except ContractValidationError, TimeoutError, ClientDisconnect:
            return profile_error("invalid_profile_request", request_id, operation=operation)

    async def create(request: Request) -> Response:
        return await handle(request, create=True)

    async def read(request: Request) -> Response:
        return await handle(request, create=False)

    router.add_api_route(CREATE_PATH, create, methods=["POST"], operation_id=CREATE)
    router.add_api_route(READ_PATH, read, methods=["GET"], operation_id=READ)
    return router
