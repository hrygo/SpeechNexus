"""ASGI middleware shared by operational endpoints."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Scope key for the server hook that resets a response whose client stopped
# reading. ASGI has no abort call, so the outermost middleware captures the
# server transport while it still owns it.
REQUEST_ABORT_KEY = "speechnexus.request_abort"


def require_request_abort_capture(app: Any) -> None:
    """Fail assembly when a middleware wraps receive outside the capture point.

    Starlette wraps ``send`` before any user middleware runs but hands
    ``receive`` through untouched, so the hook can only be captured by the
    outermost user middleware. A later ingress middleware that wraps
    ``receive`` would silently degrade every stalled stream to a graceful
    close, so the invariant is checked while the app is assembled.
    """
    stack = getattr(app, "user_middleware", ())
    outermost = stack[0].cls if stack else None
    if outermost is not RequestIdMiddleware:
        name = getattr(outermost, "__name__", outermost)
        raise RuntimeError(
            "RequestIdMiddleware must stay the outermost middleware so the "
            f"stalled-stream abort hook can capture the transport, found {name}"
        )


class RequestIdMiddleware:
    """Attach a validated or generated correlation ID to each HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_PATTERN.fullmatch(incoming) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        # A graceful transport close only finishes once the blocked write
        # buffer drains, which never happens for a peer that stopped reading.
        # Starlette wraps send in its own outermost middleware, but receive is
        # the server's bound method and still exposes the request cycle.
        cycle = getattr(receive, "__self__", None)
        transport = getattr(cycle, "transport", None)
        abort = getattr(transport, "abort", None)
        if abort is not None:

            def abort_stalled_response() -> None:
                if cycle is not None and hasattr(cycle, "disconnected"):
                    cycle.disconnected = True
                abort()

            scope[REQUEST_ABORT_KEY] = abort_stalled_response

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        await self._app(scope, receive, send_with_request_id)
