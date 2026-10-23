"""Bound concurrent uploads and total multipart bytes before parser allocation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from tempfile import SpooledTemporaryFile
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from speechnexus_backend.speech.interfaces.http.asset_errors import asset_error


class AssetIngress:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        concurrent_uploads: int,
        receive_timeout_seconds: float,
        is_enabled: Callable[[], bool] = lambda: True,
        allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1"),
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        if not 1 <= max_bytes <= 4 * 1024 * 1024 or not 1 <= concurrent_uploads <= 4:
            raise ValueError("Asset ingress must be bounded")
        if not 0 < receive_timeout_seconds <= 120:
            raise ValueError("Asset receive deadline must be bounded")
        self._app = app
        self._limit = max_bytes + 16384
        self._slots = asyncio.Semaphore(concurrent_uploads)
        self._timeout = receive_timeout_seconds
        self._enabled = is_enabled
        self._hosts = frozenset(allowed_hosts)
        self._origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        prefixes = ("/v1/nexus/assets", scope.get("root_path", "") + "/v1/nexus/assets")
        if scope["type"] != "http" or not any(
            scope["path"] == prefix or scope["path"].startswith(prefix + "/") for prefix in prefixes
        ):
            await self._app(scope, receive, send)
            return
        request_id = scope["state"]["request_id"]
        if not self._enabled():
            await asset_error("asset_service_unavailable", request_id)(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host_values, origins = headers.getlist("host"), headers.getlist("origin")
        host = None
        try:
            if (
                len(host_values) == 1
                and len(host_values[0]) <= 300
                and not any(ord(char) <= 32 or ord(char) >= 127 for char in host_values[0])
            ):
                authority = urlsplit("//" + host_values[0])
                # Accessing port also rejects malformed/out-of-range ports.
                if (authority.port is None or authority.port > 0) and not any(
                    (
                        authority.username,
                        authority.password,
                        authority.path,
                        authority.query,
                        authority.fragment,
                    )
                ):
                    host = authority.hostname
        except ValueError:
            pass
        if host not in self._hosts:
            await asset_error("asset_host_rejected", request_id)(scope, receive, send)
            return
        if len(origins) > 1 or (origins and origins[0] not in self._origins):
            await asset_error("asset_origin_rejected", request_id)(scope, receive, send)
            return
        if scope["method"] != "POST" or scope["path"] not in prefixes:
            await self._app(scope, receive, send)
            return
        lengths = headers.getlist("content-length")
        if len(lengths) > 1 or (
            lengths
            and (len(lengths[0]) > 20 or not lengths[0].isascii() or not lengths[0].isdecimal())
        ):
            await asset_error("invalid_asset_request", request_id)(scope, receive, send)
            return
        if lengths and int(lengths[0]) > self._limit:
            await asset_error("asset_too_large", request_id)(scope, receive, send)
            return
        # No wait queue or local lock masquerading as global coordination: this
        # semaphore accounts only for this process's private receive buffers.
        if self._slots.locked():
            await asset_error("asset_ingress_full", request_id)(scope, receive, send)
            return
        await self._slots.acquire()
        try:
            # Fully bounded private buffering precedes Starlette's file parser.
            # Its tempfile cleanup then sees EOF or its own parser errors, never
            # an injected size-limit exception halfway through an allocated file.
            with SpooledTemporaryFile(max_size=min(self._limit, 1024 * 1024), mode="w+b") as body:
                size, complete = 0, False
                try:
                    async with asyncio.timeout(self._timeout):
                        while not complete:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            if message["type"] != "http.request":
                                continue
                            chunk = message.get("body", b"")
                            size += len(chunk)
                            if size > self._limit:
                                await asset_error("asset_too_large", request_id)(
                                    scope, receive, send
                                )
                                return
                            body.write(chunk)
                            complete = not message.get("more_body", False)
                except TimeoutError:
                    await asset_error("asset_receive_timeout", request_id)(scope, receive, send)
                    return
                except OSError:
                    await asset_error("asset_service_unavailable", request_id)(scope, receive, send)
                    return
                if lengths and size != int(lengths[0]):
                    await asset_error("invalid_asset_request", request_id)(scope, receive, send)
                    return
                body.seek(0)
                delivered = False

                async def replay() -> Message:
                    nonlocal delivered
                    if delivered:
                        return await receive()
                    chunk = body.read(65536)
                    more = body.tell() < size
                    delivered = not more
                    return {"type": "http.request", "body": chunk, "more_body": more}

                await self._app(scope, replay, send)
        finally:
            self._slots.release()
