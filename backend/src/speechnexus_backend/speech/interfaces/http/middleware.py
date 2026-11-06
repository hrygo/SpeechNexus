"""ASGI middleware shared by operational endpoints."""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from types import SimpleNamespace
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
    """Probe abort capture through the complete, final framework middleware stack.

    Build an application copy with a synthetic endpoint in place of its router,
    without touching services or the live middleware stack. Transparent outer
    layers and capture subclasses are safe; losing the server cycle before
    capture must fail assembly. This guard must be create_app's last step:
    adding middleware afterwards does not rerun it.
    """
    aborted: list[bool] = []

    class ProbeCycle:
        transport = SimpleNamespace(abort=lambda: aborted.append(True))
        disconnected = False

        async def receive(self) -> Message:
            return {"type": "http.disconnect"}

    cycle = ProbeCycle()
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
        "app": app,
        "path": "/v1/nexus/jobs/00000000-0000-0000-0000-000000000000/events",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"accept", b"text/event-stream")],
        "state": {},
    }

    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        abort = scope.get(REQUEST_ABORT_KEY)
        if callable(abort):
            abort()

    async def send(message: Message) -> None:
        pass

    async def probe() -> None:
        probe_app = copy(app)
        probe_app.router = endpoint
        stack = probe_app.build_middleware_stack()
        await asyncio.wait_for(stack(scope, cycle.receive, send), timeout=1)

    def run_probe() -> None:
        asyncio.run(probe())

    try:
        # create_app is synchronous but can also be called from an active loop.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(run_probe).result()
    except Exception as error:
        raise RuntimeError(f"middleware probe failed: {type(error).__name__}: {error}") from error
    if not aborted or not cycle.disconnected:
        raise RuntimeError(
            "RequestIdMiddleware must stay the outermost receive capture so the "
            "stalled-stream abort hook can capture the transport; "
            "the synthetic server transport was not aborted"
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
