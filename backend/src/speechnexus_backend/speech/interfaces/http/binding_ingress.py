"""Bound binding receive buffers, while global configuration coordination stays in PG."""

from __future__ import annotations

import re
from typing import Any

from speechnexus_contracts.binding_http import contract
from starlette.types import ASGIApp

from speechnexus_backend.speech.interfaces.http.binding_errors import CREATE, READ, ingress_error
from speechnexus_backend.speech.interfaces.http.draft_ingress import DraftIngress

PREFIX = "/v1/nexus/admin/model-bindings"
CREATE_PATH = next(path for path, item in contract()["paths"].items() if "post" in item)
READ_PATH = next(path for path, item in contract()["paths"].items() if "get" in item)
READ_PATTERN = re.compile(
    re.escape(READ_PATH).replace(r"\{binding_id\}", r"[^/]+").replace(r"\{revision\}", r"[^/]+")
)


def target(path: str) -> tuple[str, str] | None:
    if path == CREATE_PATH:
        return CREATE, "POST"
    if READ_PATTERN.fullmatch(path):
        return READ, "GET"
    return None


class BindingIngress(DraftIngress):
    def __init__(self, app: ASGIApp, /, **kwargs: Any) -> None:
        super().__init__(
            app,
            prefix=PREFIX,
            target=target,
            error=ingress_error,
            create_operation=CREATE,
            read_operation=READ,
            **kwargs,
        )
