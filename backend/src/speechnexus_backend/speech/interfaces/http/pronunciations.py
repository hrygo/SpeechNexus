"""Source-bound fixed reads with bounded capacity retained until the handler ends."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError, parse
from speechnexus_contracts.pronunciation_read_http import (
    contract,
    validate_request,
    validate_response,
)
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from speechnexus_backend.speech.application.pronunciation_drafts import PronunciationReader
from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract
from speechnexus_backend.speech.interfaces.http.origin_policy import rejected_authority
from speechnexus_backend.speech.interfaces.pronunciation_drafts import draft_to_wire

PREFIX = "/v1/nexus/pronunciation-sets"
PATH = next(iter(contract()["paths"]))
OPERATION = contract()["paths"][PATH]["get"]["operationId"]
_PATTERN = re.compile(
    re.escape(PATH).replace(r"\{pronunciation_id\}", r"[^/]+").replace(r"\{revision\}", r"[^/]+")
)
_ERRORS = {
    "invalid_request": (400, "invalid_request_error", "Invalid pronunciation request."),
    "host_rejected": (403, "permission_error", "The pronunciation Host is not allowed."),
    "origin_rejected": (403, "permission_error", "The pronunciation Origin is not allowed."),
    "method_not_allowed": (405, "invalid_request_error", "This path accepts GET only."),
    "read_full": (429, "rate_limit_error", "Pronunciation read capacity is full."),
    "not_found": (404, "not_found_error", "The fixed pronunciation revision was not found."),
    "unavailable": (503, "service_unavailable_error", "Fixed pronunciation metadata unavailable."),
}


def pronunciation_http_contract() -> dict[str, Any]:
    return namespaced_contract(contract(), "PronunciationRead_")


def pronunciation_error(code: str, request_id: str) -> JSONResponse:
    status, kind, message = _ERRORS[code]
    retryable = code in ("unavailable", "read_full")
    headers = {"Cache-Control": "no-store", "X-Should-Retry": str(retryable).lower()}
    if code == "method_not_allowed":
        headers["Allow"] = "GET"
    body = dict(
        parse(
            "ErrorResponse",
            {
                "error": {
                    "code": "pronunciation_" + code,
                    "type": kind,
                    "message": message,
                    "param": None,
                    "request_id": request_id,
                    "retryable": retryable,
                }
            },
        )
    )
    validate_response(OPERATION, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


def _read(provider: PronunciationReader, envelope: dict[str, Any], request_id: str) -> JSONResponse:
    try:
        validate_request(OPERATION, envelope)
    except ContractValidationError, ValueError, TypeError, RecursionError:
        return pronunciation_error("invalid_request", request_id)
    try:
        identity, revision = envelope["path"]["pronunciation_id"], envelope["path"]["revision"]
        draft = provider.get(identity, revision)
        if draft is None:
            return pronunciation_error("not_found", request_id)
        if draft.pronunciation_id != identity or draft.revision != revision:
            raise ValueError("Wrong fixed revision")
        body = draft_to_wire(draft)
        validate_response(OPERATION, 200, {"body": body})
        return JSONResponse(body, headers={"Cache-Control": "no-store"})
    except Exception:
        return pronunciation_error("unavailable", request_id)


def create_pronunciation_router(
    get_provider: Callable[[], PronunciationReader | None],
) -> APIRouter:
    router = APIRouter(tags=["pronunciations"])

    async def read(request: Request) -> JSONResponse:
        request_id = str(request.state.request_id)
        provider = get_provider()
        if provider is None:
            return pronunciation_error("unavailable", request_id)
        pairs, ids = (
            list(request.query_params.multi_items()),
            request.headers.getlist("x-request-id"),
        )
        if len(pairs) != len({key for key, _ in pairs}) or len(ids) > 1:
            return pronunciation_error("invalid_request", request_id)
        envelope = {
            "path": request.path_params,
            "query": dict(pairs),
            "headers": {"x-request-id": ids[0]} if ids else {},
        }
        return await run_in_threadpool(_read, provider, envelope, request_id)

    router.add_api_route(PATH, read, methods=["GET"], operation_id=OPERATION)
    return router


class PronunciationIngress:
    def __init__(
        self,
        app: ASGIApp,
        *,
        concurrency: int,
        is_enabled: Callable[[], bool],
        allowed_hosts: tuple[str, ...],
        allowed_origins: tuple[str, ...],
    ) -> None:
        if not 1 <= concurrency <= 4:
            raise ValueError("Pronunciation reads must be bounded")
        self._app, self._enabled = app, is_enabled
        self._slots = asyncio.Semaphore(concurrency)
        self._hosts, self._origins = frozenset(allowed_hosts), frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "").removeprefix(scope.get("root_path", ""))
        if scope["type"] != "http" or (path != PREFIX and not path.startswith(PREFIX + "/")):
            await self._app(scope, receive, send)
            return

        async def refuse(code: str) -> None:
            await pronunciation_error(code, scope["state"]["request_id"])(scope, receive, send)

        headers = Headers(scope=scope)
        rejected = rejected_authority(headers, self._hosts, self._origins)
        if rejected is not None:
            await refuse(rejected + "_rejected")
            return
        if (
            len(path) > 512
            or len(scope.get("query_string", b"")) > 4096
            or not _PATTERN.fullmatch(path)
        ):
            await refuse("invalid_request")
            return
        if scope["method"] != "GET":
            await refuse("method_not_allowed")
            return
        if not self._enabled():
            await refuse("unavailable")
            return
        lengths = headers.getlist("content-length")
        if (lengths and lengths != ["0"]) or headers.getlist("transfer-encoding"):
            await refuse("invalid_request")
            return
        if self._slots.locked():
            await refuse("read_full")
            return
        await self._slots.acquire()
        try:
            try:
                async with asyncio.timeout(1):
                    for _ in range(8):
                        message = await receive()
                        if message["type"] != "http.request" or message.get("body"):
                            await refuse("invalid_request")
                            return
                        if not message.get("more_body", False):
                            break
                    else:
                        await refuse("invalid_request")
                        return
            except TimeoutError:
                await refuse("invalid_request")
                return
            delivered = False

            async def replay() -> Message:
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}

            processing = asyncio.ensure_future(self._app(scope, replay, send))
            try:
                await asyncio.shield(processing)
            except asyncio.CancelledError:
                # HTTP cancellation does not stop a running DB thread. Retain
                # its permit until the actual owned handler has quiesced.
                while not processing.done():
                    try:
                        await asyncio.shield(processing)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                with suppress(BaseException):
                    processing.result()
                raise
        finally:
            self._slots.release()
