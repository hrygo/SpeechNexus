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
LIST_OPERATION = "listModelBindings"
RETIRE_OPERATION = "retireModelBindingRevision"
ACTIVATE_OPERATION = "activatePublishedModelBindingRevision"
_OPERATION_PATHS = {
    specification["operationId"]: path
    for path, item in contract()["paths"].items()
    for specification in item.values()
    if isinstance(specification, dict) and "operationId" in specification
}
RETIRE_PATH = _OPERATION_PATHS[RETIRE_OPERATION]
ACTIVATE_PATH = _OPERATION_PATHS[ACTIVATE_OPERATION]


def _pattern(path: str) -> re.Pattern[str]:
    return re.compile(
        re.escape(path).replace(r"\{binding_id\}", r"[^/]+").replace(r"\{revision\}", r"[^/]+")
    )


READ_PATTERN = _pattern(READ_PATH)
RETIRE_PATTERN = _pattern(RETIRE_PATH)
ACTIVATE_PATTERN = _pattern(ACTIVATE_PATH)
DEFAULTS_PATH = next(path for path, item in defaults_contract()["paths"].items() if "get" in item)
DEFAULTS_PATTERN = re.compile(
    re.escape(DEFAULTS_PATH).replace(r"\{binding_id\}", r"[^/]+").replace(r"\{revision\}", r"[^/]+")
)


def target(path: str, method: str, *, producer: bool = False) -> tuple[str, str] | None:
    if path == CREATE_PATH:
        # The collection is both the draft create (POST) and the admin list
        # (GET); an unsupported method reports both allowed methods.
        if method == "GET":
            return LIST_OPERATION, "GET"
        if method == "POST":
            return CREATE, "POST"
        return LIST_OPERATION, "GET, POST"
    if READ_PATTERN.fullmatch(path):
        return READ, "GET"
    if RETIRE_PATTERN.fullmatch(path):
        return RETIRE_OPERATION, "POST"
    if ACTIVATE_PATTERN.fullmatch(path):
        return ACTIVATE_OPERATION, "POST"
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
            read_operations=(LIST_OPERATION, READ_DEFAULTS) if producer else (LIST_OPERATION,),
            write_operations=(RETIRE_OPERATION, ACTIVATE_OPERATION),
            **kwargs,
        )
