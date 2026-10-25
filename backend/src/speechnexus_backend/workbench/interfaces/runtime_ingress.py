"""Authority checks for the independently exposed workbench HTTP service."""

from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from speechnexus_backend.workbench.interfaces.http_ingress import request_id
from speechnexus_backend.workbench.interfaces.http_versions import _error


class RuntimeIngress:
    def __init__(
        self, app: ASGIApp, *, allowed_hosts: tuple[str, ...], allowed_origins: tuple[str, ...]
    ) -> None:
        self._app, self._hosts, self._origins = app, allowed_hosts, allowed_origins

    def _host(self, value: str) -> bool:
        try:
            parsed = urlsplit("//" + value)
            return (
                bool(value)
                and all(33 <= ord(c) <= 126 for c in value)
                and not value.endswith(":")
                and parsed.netloc == value
                and parsed.hostname in self._hosts
                and parsed.username is None
                and parsed.password is None
                and not (parsed.path or parsed.query or parsed.fragment)
                and parsed.port != 0
            )
        except ValueError:
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            hosts, origins = request.headers.getlist("host"), request.headers.getlist("origin")
            if (
                len(hosts) != 1
                or not self._host(hosts[0])
                or len(origins) > 1
                or (origins and origins[0] not in self._origins)
            ):
                await _error("invalid_request", request_id(request), None)(scope, receive, send)
                return
        await self._app(scope, receive, send)
