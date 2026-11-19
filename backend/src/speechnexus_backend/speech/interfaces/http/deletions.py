"""Deletion preview, execution and read routes.

All three operations own only speech resources, re-check the immutable plan under
one lock order (preview and execution) and never hand-write a wire shape: requests
are parsed into domain values and answers are projected back through the frozen
contract's deletion schema. The read route projects the durable ledger only; it
neither replays the plan nor resumes the object purge.
"""

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
    DeletionRequest,
    DeletionTarget,
    deletion_payload,
    identifier,
    plan_payload,
)
from speechnexus_backend.speech.interfaces.http.deletion_errors import deletion_error

PREFIX = "/v1/nexus/deletion-plans"
DELETIONS = "/v1/nexus/deletions"
# A preview body is two enums and one reference; anything larger is not this
# operation's request and never reaches JSON decoding.
MAX_REQUEST_BYTES = 16 * 1024
# A confirmation names every target the plan pinned; the bound is the plan's own
# dependency limit, not an unbounded client array.
MAX_CONFIRMATION_BYTES = 64 * 1024
_REVISION = re.compile(r"^[1-9][0-9]{0,18}$")


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Deletion body has an unexpected shape")
    return cast(dict[str, Any], value)


def _target_document(document: Any) -> DeletionTarget:
    raw = _object(document, {"owner", "kind", "resource"})
    owner, kind = raw["owner"], raw["kind"]
    if not isinstance(owner, str) or owner not in OWNERS:
        raise ValueError("Invalid deletion owner")
    if not isinstance(kind, str) or kind not in (*SPEECH_KINDS, *WORKBENCH_KINDS):
        raise ValueError("Invalid deletion kind")
    resource = _object(raw["resource"], {"id", "revision"})
    resource_id, revision = resource["id"], resource["revision"]
    if not isinstance(resource_id, str):
        raise ValueError("Invalid deletion resource identifier")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise ValueError("Invalid deletion resource revision")
    return DeletionTarget(owner, kind, resource_id, int(revision))


def _target(document: Any) -> tuple[DeletionTarget, str]:
    body = _object(document, {"target", "mode"})
    mode = body["mode"]
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("Invalid deletion mode")
    return _target_document(body["target"]), mode


def _confirmation(document: Any) -> DeletionRequest:
    body = _object(
        document, {"plan_id", "expected_plan_revision", "plan_digest", "confirm_targets"}
    )
    plan_id = body["plan_id"]
    if not isinstance(plan_id, str):
        raise ValueError("Invalid deletion plan identifier")
    expected = body["expected_plan_revision"]
    if not isinstance(expected, str) or _REVISION.fullmatch(expected) is None:
        raise ValueError("Invalid expected plan revision")
    digest = body["plan_digest"]
    if not isinstance(digest, str):
        raise ValueError("Invalid deletion plan digest")
    confirmed = body["confirm_targets"]
    if not isinstance(confirmed, list) or not confirmed:
        raise ValueError("A deletion confirmation names at least one target")
    targets = tuple(_target_document(item) for item in confirmed)
    if len(set(targets)) != len(targets):
        # A duplicate is not a set member twice; the contract declares
        # `uniqueItems`, so this is an invalid request, not a scope mismatch.
        raise ValueError("A deletion confirmation names distinct targets")
    return DeletionRequest(identifier(plan_id), int(expected), digest, targets)


def _idempotency_key(request: Request) -> str | None:
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1 or not 1 <= len(keys[0]) <= 128:
        return None
    return keys[0]


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
        key = _idempotency_key(request)
        if key is None:
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
            plan = await run_in_threadpool(service.preview, target, mode, key)
        except DeletionError as error:
            return deletion_error(error.code, request_id)
        return JSONResponse(plan_payload(plan), headers={"Cache-Control": "no-store"})

    @router.post(
        DELETIONS,
        operation_id="speechCreateDeletion",
        summary="Confirm a plan and execute the deletion",
        status_code=202,
    )
    async def create_deletion(request: Request) -> Response:
        request_id = _request_id(request)
        service = get_service()
        if service is None:
            return deletion_error("deletion_unavailable", request_id)
        key = _idempotency_key(request)
        if key is None:
            return deletion_error("invalid_idempotency_key", request_id, param="Idempotency-Key")
        body = await request.body()
        if len(body) > MAX_CONFIRMATION_BYTES:
            return deletion_error("invalid_deletion_request", request_id)
        try:
            confirmation = _confirmation(json.loads(body))
        except ValueError, TypeError:
            return deletion_error("invalid_deletion_request", request_id)
        try:
            record = await run_in_threadpool(service.execute, confirmation, key, request_id)
        except DeletionError as error:
            return deletion_error(error.code, request_id)
        return JSONResponse(
            deletion_payload(record),
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @router.get(
        DELETIONS + "/{deletion_id}",
        operation_id="speechGetDeletion",
        summary="Read a deletion record",
    )
    async def get_deletion(request: Request, deletion_id: str) -> Response:
        request_id = _request_id(request)
        service = get_service()
        if service is None:
            return deletion_error("deletion_unavailable", request_id)
        try:
            # An identifier the contract cannot express is answered like any other
            # unknown one: the read surface never confirms what it does not hold.
            identifier(deletion_id)
        except ValueError:
            return deletion_error("resource_not_found", request_id)
        try:
            record = await run_in_threadpool(service.read, deletion_id)
        except DeletionError as error:
            return deletion_error(error.code, request_id)
        return JSONResponse(deletion_payload(record), headers={"Cache-Control": "no-store"})

    return router


__all__ = ["DELETIONS", "PREFIX", "create_deletion_router"]
