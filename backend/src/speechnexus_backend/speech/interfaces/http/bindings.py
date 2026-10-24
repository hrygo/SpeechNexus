"""Actual draft POST and pinned GET; uncertainty never becomes absence or a new key."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.binding_http import validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from speechnexus_backend.speech.application.binding_documents import encode_json, parse_integer
from speechnexus_backend.speech.application.binding_drafts import BindingDrafts
from speechnexus_backend.speech.domain.binding_drafts import BindingUnavailableError
from speechnexus_backend.speech.interfaces.binding_drafts import (
    draft_to_wire,
    prepare_binding_draft,
)
from speechnexus_backend.speech.interfaces.http.binding_errors import CREATE, READ, binding_error
from speechnexus_backend.speech.interfaces.http.binding_ingress import CREATE_PATH, READ_PATH


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
    provider: BindingDrafts, envelope: dict[str, Any], raw: bytes, request_id: str
) -> Response:
    try:
        body = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_int=parse_integer,
        )
        envelope = {**envelope, "body": body}
        validate_request(CREATE, envelope)
    except ValueError, TypeError, RecursionError:
        return binding_error("invalid_binding_request", request_id, operation=CREATE)
    try:
        command = prepare_binding_draft(body, envelope["headers"].get("idempotency-key"))
    except ContractValidationError:
        return binding_error("invalid_binding_definition", request_id, operation=CREATE)
    try:
        result = provider.create(command)
        if result.outcome == "conflict":
            if result.error_code != "idempotency_conflict":
                raise BindingUnavailableError
            return binding_error("idempotency_conflict", request_id, operation=CREATE)
        if result.outcome not in ("created", "replayed") or result.draft is None:
            raise BindingUnavailableError
        if (
            result.draft.binding_id != command.binding_id
            or result.draft.definition != command.definition
        ):
            raise BindingUnavailableError
        wire = draft_to_wire(result.draft)
        validate_response(CREATE, 201, {"body": wire})
        return Response(
            encode_json(wire, max_bytes=1048576),
            status_code=201,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        # No raw driver/validation diagnostics or caller data leave this boundary.
        return binding_error("binding_result_unknown", request_id, operation=CREATE)


def _read(provider: BindingDrafts, envelope: dict[str, Any], request_id: str) -> Response:
    try:
        validate_request(READ, envelope)
    except ContractValidationError, ValueError, TypeError, RecursionError:
        return binding_error("invalid_binding_request", request_id, operation=READ)
    try:
        identity, revision = envelope["path"]["binding_id"], envelope["path"]["revision"]
        draft = provider.get(identity, revision)
        if draft is None:
            return binding_error("binding_not_found", request_id, operation=READ)
        if draft.binding_id != identity or draft.revision != revision:
            raise BindingUnavailableError
        wire = draft_to_wire(draft)
        validate_response(READ, 200, {"body": wire})
        return Response(
            encode_json(wire, max_bytes=1048576),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        return binding_error("binding_unavailable", request_id, operation=READ)


def create_binding_router(get_provider: Callable[[], BindingDrafts | None]) -> APIRouter:
    router = APIRouter(tags=["bindings"])

    async def handle(request: Request, *, create: bool) -> Response:
        operation, request_id = (CREATE if create else READ), str(request.state.request_id)
        provider = get_provider()
        if provider is None:
            return binding_error(
                "binding_result_unknown" if create else "binding_unavailable",
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
                raw = await request.body()  # BindingIngress has bounded the actual bytes and time.
                return await run_in_threadpool(_create, provider, envelope, raw, request_id)
            lengths = request.headers.getlist("content-length")
            if (
                len(lengths) > 1
                or (lengths and lengths != ["0"])
                or request.headers.getlist("transfer-encoding")
            ):
                raise ContractValidationError("A fixed binding read has no body")
            async with asyncio.timeout(1):
                for _ in range(8):
                    message = await request.receive()
                    if message["type"] != "http.request" or message.get("body"):
                        raise ContractValidationError("A fixed binding read has no body")
                    if not message.get("more_body", False):
                        break
                else:
                    raise ContractValidationError("Read receive budget exceeded")
            return await run_in_threadpool(_read, provider, envelope, request_id)
        except ContractValidationError, TimeoutError, ClientDisconnect:
            return binding_error("invalid_binding_request", request_id, operation=operation)

    async def create(request: Request) -> Response:
        return await handle(request, create=True)

    async def read(request: Request) -> Response:
        return await handle(request, create=False)

    router.add_api_route(CREATE_PATH, create, methods=["POST"], operation_id=CREATE)
    router.add_api_route(READ_PATH, read, methods=["GET"], operation_id=READ)
    return router
