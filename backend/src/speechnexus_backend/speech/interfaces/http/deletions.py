"""Only the implemented deletion-preview operation; no execution or ledger here."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, cast

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.deletions import DeletionPlanner
from speechnexus_backend.speech.domain.deletion import (
    MODES,
    OWNERS,
    SPEECH_KINDS,
    WORKBENCH_KINDS,
    DeletionError,
    DeletionTarget,
    plan_payload,
)
from speechnexus_backend.speech.interfaces.http.deletion_errors import deletion_error

PREFIX = "/v1/nexus/deletion-plans"
# A preview body is two enums and one reference; anything larger is not this
# operation's request and never reaches JSON decoding.
MAX_REQUEST_BYTES = 16 * 1024
_REVISION = re.compile(r"^[1-9][0-9]{0,18}$")


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Deletion plan body has an unexpected shape")
    return cast(dict[str, Any], value)


def _target(document: Any) -> tuple[DeletionTarget, str]:
    body = _object(document, {"target", "mode"})
    mode = body["mode"]
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("Invalid deletion mode")
    raw = _object(body["target"], {"owner", "kind", "resource"})
    owner, kind = raw["owner"], raw["kind"]
    if not isinstance(owner, str) or owner not in OWNERS:
        raise ValueError("Invalid deletion owner")
    if not isinstance(kind, str) or kind not in (*SPEECH_KINDS, *WORKBENCH_KINDS):
        raise ValueError("Invalid deletion kind")
    resource = _object(raw["resource"], {"id", "revision"})
    identifier, revision = resource["id"], resource["revision"]
    if not isinstance(identifier, str):
        raise ValueError("Invalid deletion resource identifier")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise ValueError("Invalid deletion resource revision")
    return DeletionTarget(owner, kind, identifier, int(revision)), mode


def create_deletion_router(get_service: Callable[[], DeletionPlanner | None]) -> APIRouter:
    router = APIRouter(tags=["deletions"])

    @router.post(
        PREFIX,
        operation_id="speechCreateDeletionPlan",
        summary="Create an immutable deletion plan",
    )
    async def create_deletion_plan(request: Request) -> Response:
        request_id = _request_id(request)
        service = get_service()
        if service is None:
            return deletion_error("deletion_plans_unavailable", request_id)
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1 or not 1 <= len(keys[0]) <= 128:
            return deletion_error("invalid_idempotency_key", request_id, param="Idempotency-Key")
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return deletion_error("invalid_deletion_request", request_id)
        try:
            target, mode = _target(json.loads(body))
        except ValueError, TypeError:
            # json.JSONDecodeError is a ValueError; fixed text never echoes input.
            return deletion_error("invalid_deletion_request", request_id)
        try:
            plan = await run_in_threadpool(service.preview, target, mode, keys[0])
        except DeletionError as error:
            return deletion_error(error.code, request_id)
        return JSONResponse(plan_payload(plan), headers={"Cache-Control": "no-store"})

    return router


__all__ = ["PREFIX", "create_deletion_router"]
