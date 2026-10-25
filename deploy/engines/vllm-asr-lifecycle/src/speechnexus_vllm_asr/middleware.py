"""Cover the entire native ASGI request, including response-send cancellation."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from starlette._utils import get_route_path

from .scope import CURRENT_SCOPE, ScopeCapacityError


class AsrScopeMiddleware:
    def __init__(self, app: Any, *, root_app: Any) -> None:
        self.app = app
        self.root_app = root_app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or get_route_path(scope) != "/v1/audio/transcriptions"
        ):
            await self.app(scope, receive, send)
            return
        controller = self.root_app.state.speechnexus_asr_scopes
        try:
            request_scope = controller.open()
        except ScopeCapacityError:
            # Construct the vendor error through its own serving implementation.
            # This private rejection occurs before media parsing or dispatch.
            error = self.root_app.state.openai_serving_transcription.create_error_response(
                "ASR request cleanup capacity is occupied",
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            body = error.model_dump_json().encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": HTTPStatus.SERVICE_UNAVAILABLE,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        token = CURRENT_SCOPE.set(request_scope)
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_SCOPE.reset(token)
            controller.seal(request_scope)
