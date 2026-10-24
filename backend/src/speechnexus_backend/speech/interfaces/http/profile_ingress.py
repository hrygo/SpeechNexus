"""Bound profile receive buffers, while global configuration coordination stays in PG."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextlib import suppress

from speechnexus_contracts.profile_http import contract
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from speechnexus_backend.speech.interfaces.http.origin_policy import rejected_authority
from speechnexus_backend.speech.interfaces.http.profile_errors import CREATE, READ, profile_error

PREFIX = "/v1/nexus/admin/profiles"
CREATE_PATH = next(path for path, item in contract()["paths"].items() if "post" in item)
READ_PATH = next(path for path, item in contract()["paths"].items() if "get" in item)
READ_PATTERN = re.compile(
    re.escape(READ_PATH).replace(r"\{profile_id\}", r"[^/]+").replace(r"\{revision\}", r"[^/]+")
)


def target(path: str) -> tuple[str, str] | None:
    if path == CREATE_PATH:
        return CREATE, "POST"
    if READ_PATTERN.fullmatch(path):
        return READ, "GET"
    return None


class ProfileIngress:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        concurrency: int,
        receive_timeout_seconds: float,
        is_enabled: Callable[[], bool],
        allowed_hosts: tuple[str, ...],
        allowed_origins: tuple[str, ...],
    ) -> None:
        if not 1 <= max_bytes <= 1048576 or not 1 <= concurrency <= 4:
            raise ValueError("Profile ingress must be bounded")
        if not 0 < receive_timeout_seconds <= 30:
            raise ValueError("Profile receive deadline must be bounded")
        self._app, self._limit, self._timeout = app, max_bytes, receive_timeout_seconds
        self._enabled, self._slots = is_enabled, asyncio.Semaphore(concurrency)
        self._hosts, self._origins = frozenset(allowed_hosts), frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"].removeprefix(scope.get("root_path", ""))
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            await self._app(scope, receive, send)
            return
        request_id = scope["state"]["request_id"]
        selected = target(path)
        operation = selected[0] if selected else None

        async def refuse(code: str, *, allow: str | None = None) -> None:
            await profile_error(code, request_id, operation=operation, allow=allow)(
                scope, receive, send
            )

        headers = Headers(scope=scope)
        rejected = rejected_authority(headers, self._hosts, self._origins)
        if rejected is not None:
            await refuse("profile_" + rejected + "_rejected")
            return
        if selected is None or len(path) > 512 or len(scope.get("query_string", b"")) > 4096:
            await refuse("invalid_profile_request")
            return
        if scope["method"] != selected[1]:
            await refuse("profile_method_not_allowed", allow=selected[1])
            return
        if not self._enabled():
            await refuse("profile_result_unknown" if operation == CREATE else "profile_unavailable")
            return
        if operation == READ:
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
            await refuse("invalid_profile_request")
            return
        if lengths and int(lengths[0]) > self._limit:
            await refuse("profile_too_large")
            return
        if not media or media[0].partition(";")[0].strip().lower() != "application/json":
            await refuse(
                "invalid_profile_request"
                if not media and lengths == ["0"]
                else "profile_media_unsupported"
            )
            return
        # This semaphore owns only bounded process-private receive/parse buffers.
        if self._slots.locked():
            await refuse("profile_ingress_full")
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
                            await refuse("invalid_profile_request")
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > self._limit:
                            await refuse("profile_too_large")
                            return
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
                    else:
                        await refuse("invalid_profile_request")
                        return
            except TimeoutError:
                await refuse("profile_receive_timeout")
                return
            if lengths and len(body) != int(lengths[0]):
                await refuse("invalid_profile_request")
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
