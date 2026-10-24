"""One deployment Host/Origin check shared by content and group endpoints."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from starlette.datastructures import Headers


def rejected_authority(
    headers: Headers, allowed_hosts: frozenset[str], allowed_origins: frozenset[str]
) -> Literal["host", "origin"] | None:
    host_values, origins = headers.getlist("host"), headers.getlist("origin")
    host = None
    try:
        if (
            len(host_values) == 1
            and len(host_values[0]) <= 300
            and not any(char in host_values[0] for char in "/?#@")
            and not any(ord(char) <= 32 or ord(char) >= 127 for char in host_values[0])
        ):
            authority = urlsplit("//" + host_values[0])
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
    if host not in allowed_hosts:
        return "host"
    if len(origins) > 1 or (origins and origins[0] not in allowed_origins):
        return "origin"
    return None
