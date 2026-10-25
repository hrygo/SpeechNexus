"""Bound binding receive buffers, while global configuration coordination stays in PG."""

from __future__ import annotations

import re
from functools import partial
from typing import Any

from speechnexus_contracts.binding_defaults_http import contract as defaults_contract
from speechnexus_contracts.binding_http import contract
from starlette.types import ASGIApp

from speechnexus_backend.speech.interfaces.http.binding_errors import (
    CREATE,
    READ,
    READ_DEFAULTS,
    ingress_error,
)
from speechnexus_backend.speech.interfaces.http.draft_ingress import DraftIngress

PREFIX = "/v1/nexus/admin/model-bindings"
CREATE_PATH = next(path for path, item in contract()["paths"].items() if "post" in item)
READ_PATH = next(path for path, item in contract()["paths"].items() if "get" in item)
READ_PATTERN = re.compile(
    re.escape(READ_PATH).replace(r"\{binding_id\}", r"[^/]+").replace(r"\{revision\}", r"[^/]+")
)
DEFAULTS_PATH = next(path for path, item in defaults_contract()["paths"].items() if "get" in item)
DEFAULTS_PATTERN = re.compile(
    re.escape(DEFAULTS_PATH).replace(r"\{binding_id\}", r"[^/]+").replace(r"\{revision\}", r"[^/]+")
)


def target(path: str, *, producer: bool = False) -> tuple[str, str] | None:
    if path == CREATE_PATH:
        return CREATE, "POST"
    if READ_PATTERN.fullmatch(path):
        return READ, "GET"
    if producer and DEFAULTS_PATTERN.fullmatch(path):
        return READ_DEFAULTS, "GET"
    return None


class BindingIngress(DraftIngress):
    def __init__(self, app: ASGIApp, /, *, producer: bool = False, **kwargs: Any) -> None:
        super().__init__(
            app,
            prefix=PREFIX,
            target=partial(target, producer=producer),
            error=partial(ingress_error, producer=producer),
            create_operation=CREATE,
            read_operation=READ,
            read_operations=(READ_DEFAULTS,) if producer else (),
            **kwargs,
        )
