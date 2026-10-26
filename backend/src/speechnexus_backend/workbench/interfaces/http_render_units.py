"""Versioned generated HTTP operations over the private render-unit snapshot."""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.render_units import (
    RenderDispatchUnavailableError,
    RenderSubmissionConflictError,
    RenderSubmissionRejectedError,
    RenderSubmissionUnknownError,
    WorkbenchRenderUnits,
)
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.render_units import RenderAttemptConflictError
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.interfaces.cursors import CursorCodec, InvalidCursorError
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    default_limit,
    read_envelope,
    request_id,
    validate_limits,
)
from speechnexus_backend.workbench.interfaces.http_versions import _error
from speechnexus_backend.workbench.interfaces.render_unit_views import render_unit_payload

OPERATIONS = frozenset(
    name + suffix
    for name in ("listRenderUnits", "createRenderUnit", "getRenderUnit", "updateRenderUnit")
    for suffix in ("", "V2")
) | {"generateRenderUnitV2"}


def create_render_unit_router(
    service: WorkbenchRenderUnits | None,
    *,
    default_workspace_id: str,
    cursor: CursorCodec,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> APIRouter:
    validate_limits(max_json_bytes, body_timeout_seconds)
    parse(
        "RenderUnitCreateRequestV2",
        cast(
            Any,
            {
                "ordinal": 0,
                "text": "synthetic",
                "voice": {"id": "voice_demo", "revision": "1"},
                "model": "synthetic",
                "model_binding": {"id": "binding_demo", "revision": "1"},
            },
        ),
    )
    document = contract()
    limits = {
        op["operationId"]: default_limit(document, op)
        for item in document["paths"].values()
        for op in item.values()
        if op["operationId"] in {"listRenderUnits", "listRenderUnitsV2"}
    }
    router = APIRouter()

    async def dispatch(operation: str, value: dict[str, Any]) -> tuple[int, Any]:
        # These options have wire contracts but no installed write implementation.
        # Reject the whole request, including explicit clears, before invoking a store.
        if {"pronunciation", "profile"}.intersection(value.get("body") or {}):
            raise InvalidVersionChangeError("unsupported render configuration")
        # v1 never supplied the binding required by the durable execution intent.
        # Keep its request shape, but do not invent a binding or persist a partial unit.
        if operation == "createRenderUnit":
            raise IngressError("workbench_unavailable")
        if service is None:
            raise IngressError("workbench_unavailable")
        version: Literal[1, 2] = 2 if operation.endswith("V2") else 1
        name = operation.removesuffix("V2")
        path = value["path"]
        if name == "listRenderUnits":
            query = value["query"]
            parent = path["chapter_id"]
            after = (
                None
                if "cursor" not in query
                else cursor.decode(query["cursor"], kind="render_units", parent_id=parent)
            )
            limit = int(query["limit"]) if "limit" in query else limits[operation]
            units_page = await service.list_units(
                chapter_id=parent, workspace_id=default_workspace_id, limit=limit, after=after
            )
            next_position = units_page.next_position
            return 200, {
                "object": "nexus.list",
                "data": [
                    render_unit_payload(item.unit, version=version) for item in units_page.items
                ],
                "has_more": next_position is not None,
                "next_cursor": None if next_position is None else cursor.encode(next_position),
            }
        if name == "getRenderUnit":
            return 200, render_unit_payload(
                await service.get_unit(unit_id=path["unit_id"], workspace_id=default_workspace_id),
                version=version,
            )
        body = value["body"]
        if name == "generateRenderUnit":
            body = value["body"]
            started, job_id = await service.start_unit(
                unit_id=path["unit_id"],
                workspace_id=default_workspace_id,
                expected_revision=int(body["expected_unit_revision"]),
                key=value["headers"].get("idempotency-key"),
            )
            return 202, {
                "render_unit": render_unit_payload(started, version=2),
                "speech_job_id": job_id,
                "accepted_at": started.updated_at.isoformat(),
            }
        if name == "updateRenderUnit":
            voice = body.get("voice")
            binding = body.get("model_binding")
            return 200, render_unit_payload(
                await service.update_unit(
                    unit_id=path["unit_id"],
                    workspace_id=default_workspace_id,
                    expected_revision=int(body["expected_revision"]),
                    text=body.get("text"),
                    voice_id=None if voice is None else voice["id"],
                    voice_revision=None if voice is None else int(voice["revision"]),
                    model=body.get("model"),
                    model_binding_id=None if binding is None else binding["id"],
                    model_binding_revision=None if binding is None else int(binding["revision"]),
                ),
                version=version,
            )
        voice = body["voice"]
        binding = body["model_binding"]
        return 201, render_unit_payload(
            await service.create_unit(
                workspace_id=default_workspace_id,
                chapter_id=path["chapter_id"],
                ordinal=body["ordinal"],
                text=body["text"],
                voice_id=voice["id"],
                voice_revision=int(voice["revision"]),
                model=body["model"],
                model_binding_id=binding["id"],
                model_binding_revision=int(binding["revision"]),
                key=value["headers"].get("idempotency-key"),
            )
        )

    def endpoint(operation: str, has_body: bool) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            rid = request_id(request)
            try:
                value = await read_envelope(
                    request,
                    operation,
                    has_body=has_body,
                    max_json_bytes=max_json_bytes,
                    body_timeout_seconds=body_timeout_seconds,
                )
                status, body = await dispatch(operation, value)
                headers = {"X-Request-ID": rid, "Cache-Control": "no-store"}
                validate_response(operation, status, {"body": body, "headers": headers})
                return JSONResponse(body, status_code=status, headers=headers)
            except IngressError as error:
                return _error(error.code, rid, operation)
            except InvalidCursorError:
                return _error("invalid_cursor", rid, operation)
            except ResourceNotFoundError:
                return _error("resource_not_found", rid, operation)
            except IdempotencyConflictError:
                return _error("idempotency_conflict", rid, operation)
            except RenderDispatchUnavailableError:
                return _error("workbench_unavailable", rid, operation)
            except RenderSubmissionUnknownError:
                return _error("outcome_unknown", rid, operation)
            except RenderSubmissionConflictError:
                return _error("idempotency_conflict", rid, operation)
            except RenderSubmissionRejectedError:
                return _error("render_submission_rejected", rid, operation)
            except RenderAttemptConflictError:
                return _error("render_attempt_conflict", rid, operation)
            except RevisionConflictError:
                return _error("revision_conflict", rid, operation)
            except InvalidVersionChangeError:
                return _error("invalid_change", rid, operation)
            except VersionStoreError as error:
                return _error(
                    "outcome_unknown" if error.commit_unknown else "store_unavailable",
                    rid,
                    operation,
                )
            except Exception:
                # CancelledError is a BaseException and must reach the ASGI host.
                return _error("internal_error", rid, operation)

        return handle

    for path, item in document["paths"].items():
        for method, operation in item.items():
            name = operation["operationId"]
            if name in OPERATIONS:
                router.add_api_route(
                    path,
                    endpoint(name, "requestBody" in operation),
                    methods=[method.upper()],
                    operation_id=name,
                    response_model=None,
                )
    return router
