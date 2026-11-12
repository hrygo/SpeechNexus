"""ASGI surface for the private inspection contract; no database or object store."""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from speechnexus_asset_inspector.protocol import (
    CONTRACT_VERSION,
    HTTP_STATUS,
    RECIPE_VERSION,
    failure_body,
)
from speechnexus_asset_inspector.runner import fatal_exit
from speechnexus_asset_inspector.service import BodyProtocolError, InspectorService

_LOG = logging.getLogger(__name__)

_INSPECTION_PATH = "/internal/v1/inspection/audio"
_CONTENT_TYPE = "application/octet-stream"
_MAX_REQUEST_ID = 128


def _failure(status: str) -> Response:
    return Response(
        content=failure_body(status),
        status_code=HTTP_STATUS[status],
        media_type="application/json",
    )


def _authorized(request: Request, token: bytes) -> bool:
    header = request.headers.get("authorization")
    if header is None:
        return False
    scheme, separator, value = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not value:
        return False
    try:
        provided = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(provided, token)


def _positive_integer(value: str) -> int | None:
    if not value.isascii() or not value.isdecimal():
        return None
    number = int(value)
    return number if number >= 1 else None


def _request_id(request: Request) -> str | None:
    value = request.headers.get("x-request-id")
    if value is None:
        return None
    if not 1 <= len(value) <= _MAX_REQUEST_ID or not value.isascii():
        raise BodyProtocolError
    if not all(character.isprintable() for character in value):
        raise BodyProtocolError
    return value


def create_app(
    service: InspectorService,
    *,
    exit_process: Callable[[int], None] = fatal_exit,
) -> Starlette:
    settings = service.settings

    async def _self_check() -> None:
        try:
            passed = await service.self_check()
        except Exception:
            _LOG.exception("asset inspector self-check raised")
            passed = False
        if not passed:
            _LOG.error("asset inspector self-check failed; exiting for restart")
            exit_process(1)
            return
        service.mark_ready()
        _LOG.info("asset inspector self-check passed")

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Bind first, then self-check: readiness stays 503 until the real child
        # decodes the built-in synthetic WAV.
        task = asyncio.create_task(_self_check())
        try:
            yield
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def readyz(_request: Request) -> Response:
        if not service.ready:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse(
            {
                "contract_version": CONTRACT_VERSION,
                "recipe_version": RECIPE_VERSION,
                "max_encoded_bytes": settings.max_encoded_bytes,
                "max_wall_seconds": settings.max_wall_seconds,
                "in_flight": service.in_flight,
            }
        )

    async def inspect(request: Request) -> Response:
        if not _authorized(request, settings.token):
            return _failure("unauthorized")
        content_type = request.headers.get("content-type")
        if content_type is None or content_type.strip().lower() != _CONTENT_TYPE:
            return _failure("unsupported_media")
        raw_length = request.headers.get("content-length")
        if raw_length is None:
            return _failure("bad_request")
        declared = _positive_integer(raw_length)
        if declared is None:
            return _failure("bad_request")
        if declared > settings.max_encoded_bytes:
            return _failure("encoded_too_large")
        raw_deadline = request.headers.get("x-nexus-deadline-ms")
        deadline = None if raw_deadline is None else _positive_integer(raw_deadline)
        if deadline is None:
            return _failure("bad_request")
        raw_duration = request.headers.get("x-nexus-max-duration-ms")
        duration = None if raw_duration is None else _positive_integer(raw_duration)
        if duration is None:
            return _failure("bad_request")
        try:
            request_id = _request_id(request)
        except BodyProtocolError:
            return _failure("bad_request")
        if not service.ready:
            return _failure("unavailable")
        try:
            outcome = await service.inspect(
                _body(request),
                declared_bytes=declared,
                deadline_ms=deadline,
                max_duration_ms=duration,
            )
        except BodyProtocolError:
            return _failure("bad_request")
        except Exception:
            # No exception text, path, command or input fragment reaches the
            # caller or a content-bearing log line.
            _LOG.exception("asset inspection failed unexpectedly")
            return _failure("unavailable")
        if outcome.status != "complete":
            return _failure(outcome.status)
        response = Response(content=outcome.receipt, media_type="application/json")
        if request_id is not None:
            response.headers["X-Request-ID"] = request_id
        return response

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", readyz, methods=["GET"]),
            Route(_INSPECTION_PATH, inspect, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


async def _body(request: Request) -> AsyncIterator[bytes]:
    async for chunk in request.stream():
        yield chunk
