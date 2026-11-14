"""Versioned generated HTTP operations over the private render-unit snapshot."""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.render_units import (
    RenderDispatchUnavailableError,
    RenderRejection,
    RenderRejectionCause,
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
from speechnexus_backend.workbench.interfaces.render_unit_views import (
    render_generations_payload,
    render_unit_payload,
)

OPERATIONS = frozenset(
    name + suffix
    for name in ("listRenderUnits", "createRenderUnit", "getRenderUnit", "updateRenderUnit")
    for suffix in ("", "V2")
) | {"generateRenderUnitV2", "listRenderUnitGenerationsV2"}

# The public refusal vocabulary is keyed to the operator step, not to the upstream
# Job code that reports it: `#702`/`#703` keep revising which upstream code states
# which fact, and a 1:1 mirror would fossilize that churn into this contract. The
# upstream code is still how the adapter chooses a disposition, and the upstream
# `param` is forwarded unchanged because it is the same thing this envelope means:
# which request field the refusal names.
_INLINE_TEXT_PARAMS = frozenset({"input.text", "text"})
_REFUSAL_CODES = {
    "workspace_not_configured": "render_submission_workspace_not_configured",
    "workspace_not_found": "render_submission_workspace_unavailable",
    "workspace_disabled": "render_submission_workspace_unavailable",
    "voice_not_published": "render_submission_voice_unavailable",
    "asset_not_ready": "render_submission_asset_unavailable",
    # One step ("pin a revision this deployment can dispatch"), so one code: a pinned
    # model, binding or profile differs in the resource, not in the action.
    "model_not_published": "render_submission_binding_unavailable",
    "model_binding_not_published": "render_submission_binding_unavailable",
    "model_binding_retired": "render_submission_binding_unavailable",
    "model_binding_not_current": "render_submission_binding_unavailable",
    "model_binding_mismatch": "render_submission_binding_unavailable",
    "model_binding_task_not_supported": "render_submission_binding_unavailable",
    "job_binding_incomplete": "render_submission_binding_unavailable",
    "profile_not_published": "render_submission_binding_unavailable",
    # A pinned reference that resolves to nothing is a pinned field the operator has
    # to correct; the step is the same as any other unsupported pinned value.
    "pronunciation_not_found": "render_submission_field_unsupported",
    "job_request_too_large": "render_submission_body_too_large",
}
_FALLBACK_REFUSAL = "render_submission_rejected"

# The 409 family is one status class with three operator steps (#755): a key already
# spent on another intent ("inspect that Job or use a new key"), a previous execution
# that has not confirmed its stop ("wait, then retry" -- the one conflict worth
# retrying), and everything else. Only a code this table names may claim one of the
# first two; an unreadable 409 envelope keeps the unnamed conflict step instead of
# borrowing a step it did not prove.
_CONFLICT_CODES = {
    "idempotency_conflict": "render_submission_idempotency_conflict",
    "execution_cleanup_pending": "render_submission_cleanup_pending",
}
_CONFLICT_FALLBACK = "render_submission_conflict"

# Statuses that already name the operator's next step by themselves (#755): a 401 is
# a broken credential, a 403 a permission refusal, a 415 a refused media type, a 429
# an admission refusal that wrote no row. The status is enough even when the envelope
# cannot be read, which is why these stay out of the code table -- folding them into
# the 422 "the request is invalid" bucket is a wrong attribution, and the operator
# step for each is different.
_STATUS_REFUSALS = {
    401: "render_submission_credentials_rejected",
    403: "render_submission_permission_denied",
    415: "render_submission_media_unsupported",
    429: "render_submission_capacity_exhausted",
}

# The only refusals whose next step is "wait, then dispatch again". Stated once, so
# that a 429 or a cleanup wait can never be published as terminal and a 401/403/415
# or a spent idempotency key can never be published as retryable (#755 is where those
# two directions were confused). The route reads this set for the envelope's
# `retryable`, and the panel mirrors it for its copy.
_RETRYABLE_REFUSAL_CODES = frozenset(
    {
        "render_submission_capacity_exhausted",
        "render_submission_cleanup_pending",
    }
)


def refusal_code(rejection: RenderRejection | None) -> str:
    """The workbench disposition a dispatch refusal demands, or the unnamed fallback.

    ``render_submission_rejected`` is not a catch-all for convenience: it means the
    refusal stated no cause this workbench can act on - a unit refused locally before
    any request, an upstream envelope that could not be read, or an upstream code
    whose operator step this vocabulary does not name.
    """
    if rejection is None:
        return _FALLBACK_REFUSAL
    if rejection.cause is RenderRejectionCause.LOCAL_CONTRACT:
        # The unit never left the workbench; only the text bound can be named locally.
        return (
            "render_submission_text_too_long"
            if rejection.param in _INLINE_TEXT_PARAMS
            else _FALLBACK_REFUSAL
        )
    # A conflict is decided by its code, and by nothing else: the same status also
    # retries the previous execution's cleanup, which shares no step with a spent key.
    status = rejection.status_code
    if status == 409:
        upstream = rejection.upstream_code
        if upstream is None:
            return _CONFLICT_FALLBACK
        return _CONFLICT_CODES.get(upstream, _CONFLICT_FALLBACK)
    if status is not None:
        status_refusal = _STATUS_REFUSALS.get(status)
        if status_refusal is not None:
            return status_refusal
    if rejection.cause is RenderRejectionCause.UPSTREAM_UNREADABLE:
        return _FALLBACK_REFUSAL
    upstream = rejection.upstream_code
    if upstream is None:  # Excluded by RenderRejection; kept for a total mapping.
        return _FALLBACK_REFUSAL
    if upstream == "unsupported_parameter":
        # #745's lesson: a 422 attributes to the text only when the upstream says so.
        # The upstream public Job contract sends `param: null` for *every* error
        # (`speech/interfaces/http/job_errors.py`), so the two branches below are
        # reachable only after the upstream starts naming the parameter. Until then an
        # unsupported parameter states no cause this workbench can act on: it stays the
        # fallback, and the caller's own measurement is the only admissible
        # attribution (which is exactly what the workbench panel does, #745).
        if rejection.param is None:
            return _FALLBACK_REFUSAL
        return (
            "render_submission_text_too_long"
            if rejection.param in _INLINE_TEXT_PARAMS
            else "render_submission_field_unsupported"
        )
    if rejection.status_code == 413:
        # The deployment receive limit refuses the derived body whatever code it sends.
        return "render_submission_body_too_large"
    return _REFUSAL_CODES.get(upstream, _FALLBACK_REFUSAL)


def refusal_retryable(code: str) -> bool:
    """Whether this disposition's next step is to wait and dispatch again."""
    return code in _RETRYABLE_REFUSAL_CODES


def retry_after_seconds(rejection: RenderRejection | None, code: str) -> int | None:
    """The upstream delay, forwarded only for a disposition that may be retried."""
    if rejection is None or not refusal_retryable(code):
        return None
    return rejection.retry_after_seconds


def refusal_param(rejection: RenderRejection | None) -> str | None:
    """The field pointer the refusing layer named, forwarded only when it stated one."""
    if rejection is None or rejection.cause is RenderRejectionCause.UPSTREAM_UNREADABLE:
        return None
    return rejection.param


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
        if operation == "listRenderUnitGenerationsV2":
            limit = int(value["query"].get("limit", 20))
            unit, generations, has_more = await service.generations(
                unit_id=path["unit_id"], workspace_id=default_workspace_id, limit=limit
            )
            return 200, render_generations_payload(unit.id, generations, has_more=has_more)
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
            except RenderSubmissionRejectedError as error:
                code = refusal_code(error.rejection)
                return _error(
                    code,
                    rid,
                    operation,
                    param=refusal_param(error.rejection),
                    retryable=refusal_retryable(code),
                    retry_after_seconds=retry_after_seconds(error.rejection, code),
                )
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
