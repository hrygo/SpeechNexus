"""Shared bounded private receive buffers for independently owned draft resources."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress

from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from speechnexus_backend.speech.interfaces.http.origin_policy import rejected_authority


class DraftIngress:
    def __init__(
        self,
        app: ASGIApp,
        *,
        prefix: str,
        target: Callable[[str], tuple[str, str] | None],
        error: Callable[..., Response],
        create_operation: str,
        read_operation: str,
        read_operations: tuple[str, ...] = (),
        max_bytes: int,
        concurrency: int,
        receive_timeout_seconds: float,
        is_enabled: Callable[[], bool],
        allowed_hosts: tuple[str, ...],
        allowed_origins: tuple[str, ...],
    ) -> None:
        if not 1 <= max_bytes <= 1048576 or not 1 <= concurrency <= 4:
            raise ValueError("Draft ingress must be bounded")
        if not 0 < receive_timeout_seconds <= 30:
            raise ValueError("Draft receive deadline must be bounded")
        self._prefix, self._target, self._error = prefix, target, error
        self._create, self._reads = create_operation, frozenset((read_operation, *read_operations))
        self._app, self._limit, self._timeout = app, max_bytes, receive_timeout_seconds
        self._enabled, self._slots = is_enabled, asyncio.Semaphore(concurrency)
        self._hosts, self._origins = frozenset(allowed_hosts), frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"].removeprefix(scope.get("root_path", ""))
        if path != self._prefix and not path.startswith(self._prefix + "/"):
            await self._app(scope, receive, send)
            return
        request_id = scope["state"]["request_id"]
        selected = self._target(path)
        operation = selected[0] if selected else None

        async def refuse(code: str, *, allow: str | None = None) -> None:
            await self._error(code, request_id, operation=operation, allow=allow)(
                scope, receive, send
            )

        headers = Headers(scope=scope)
        rejected = rejected_authority(headers, self._hosts, self._origins)
        if rejected is not None:
            await refuse(rejected + "_rejected")
            return
        if selected is None or len(path) > 512 or len(scope.get("query_string", b"")) > 4096:
            await refuse("invalid_request")
            return
        if scope["method"] != selected[1]:
            await refuse("method_not_allowed", allow=selected[1])
            return
        if not self._enabled():
            await refuse("result_unknown" if operation == self._create else "unavailable")
            return
        if operation in self._reads:
            await self._app(scope, receive, send)
            return
        lengths, media = headers.getlist("content-length"), headers.getlist("content-type")
        encodings = headers.getlist("transfer-encoding")
        if (
            len(lengths) > 1
            or len(media) > 1
            or len(encodings) > 1
            or (lengths and encodings)
            or (
                lengths
                and (len(lengths[0]) > 20 or not lengths[0].isascii() or not lengths[0].isdecimal())
            )
        ):
            await refuse("invalid_request")
            return
        if lengths and int(lengths[0]) > self._limit:
            await refuse("too_large")
            return
        if not media or media[0].partition(";")[0].strip().lower() != "application/json":
            await refuse(
                "invalid_request" if not media and lengths == ["0"] else "media_unsupported"
            )
            return
        # This semaphore owns only bounded process-private receive/parse buffers.
        if self._slots.locked():
            await refuse("ingress_full")
            return
        await self._slots.acquire()
        try:
            body = bytearray()
            try:
                async with asyncio.timeout(self._timeout):
                    for _ in range(4096):
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        if message["type"] != "http.request":
                            await refuse("invalid_request")
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > self._limit:
                            await refuse("too_large")
                            return
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
                    else:
                        await refuse("invalid_request")
                        return
            except TimeoutError:
                await refuse("receive_timeout")
                return
            if lengths and len(body) != int(lengths[0]):
                await refuse("invalid_request")
                return
            raw, delivered = bytes(body), False
            body.clear()

            async def replay() -> Message:
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": raw, "more_body": False}

            processing = asyncio.ensure_future(self._app(scope, replay, send))
            try:
                await asyncio.shield(processing)
            except asyncio.CancelledError:
                # A complete request may already own a real SQL transaction.
                # Keep its local buffer permit until the owned handler ends;
                # client cancellation is not proof of rollback or absence.
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
