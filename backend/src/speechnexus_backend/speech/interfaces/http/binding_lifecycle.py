"""Source-validated binding retirement and explicit rollback endpoints."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol, cast

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin import parse as parse_admin
from speechnexus_contracts.binding_http import validate_request, validate_response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.binding_documents import encode_document
from speechnexus_backend.speech.application.binding_publication import PublishedBinding
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft
from speechnexus_backend.speech.domain.binding_lifecycle import (
    BindingActivationCommand,
    BindingActivationConflictError,
    BindingLifecycleConflictError,
    BindingLifecycleNotFoundError,
    BindingLifecycleReceipt,
    BindingLifecycleUnavailableError,
    BindingPublicationCommand,
    BindingPublicationEvidenceError,
    BindingPublicationHeadConflictError,
    BindingRetirementCommand,
)
from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.interfaces.binding_drafts import revision_to_wire
from speechnexus_backend.speech.interfaces.http.binding_ingress import (
    ACTIVATE_OPERATION as ACTIVATE,
)
from speechnexus_backend.speech.interfaces.http.binding_ingress import (
    ACTIVATE_PATH,
    PUBLISH_PATH,
    RETIRE_PATH,
)
from speechnexus_backend.speech.interfaces.http.binding_ingress import (
    PUBLISH_OPERATION as PUBLISH,
)
from speechnexus_backend.speech.interfaces.http.binding_ingress import (
    RETIRE_OPERATION as RETIRE,
)

_ERRORS = {
    "invalid_binding_request": (400, "invalid_request_error", "Invalid binding lifecycle request."),
    "binding_not_found": (404, "not_found_error", "The binding revision was not published."),
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "The key identifies another binding lifecycle intent.",
    ),
    "configuration_changed": (
        409,
        "invalid_request_error",
        "The active binding revision changed.",
    ),
    "revision_already_retired": (
        409,
        "invalid_request_error",
        "The binding revision is already retired.",
    ),
    "revision_not_current": (
        409,
        "invalid_request_error",
        "Only the active binding revision can be retired.",
    ),
    "revision_not_published": (
        409,
        "invalid_request_error",
        "Retired and disabled bindings cannot be activated.",
    ),
    "binding_not_draft": (
        409,
        "invalid_request_error",
        "Only a stored draft binding revision can be published.",
    ),
    "capacity_manifest_mismatch": (
        422,
        "invalid_request_error",
        "The capacity manifest does not describe the draft binding revision.",
    ),
    "binding_result_unknown": (
        503,
        "service_unavailable_error",
        "The binding lifecycle result is unknown.",
    ),
}


class BindingLifecycle(Protocol):
    def publish(self, command: BindingPublicationCommand) -> BindingLifecycleReceipt: ...

    def retire(self, command: BindingRetirementCommand) -> BindingLifecycleReceipt: ...

    def activate(self, command: BindingActivationCommand) -> BindingLifecycleReceipt: ...


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def _json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)


def _error(
    code: str,
    request_id: str,
    operation: str,
    *,
    retryable: bool | None = None,
    message: str | None = None,
) -> JSONResponse:
    status, kind, default_message = _ERRORS.get(code, _ERRORS["binding_result_unknown"])
    should_retry = status == 503 if retryable is None else retryable
    headers = {"Cache-Control": "no-store", "X-Should-Retry": str(should_retry).lower()}
    body = dict(
        parse_admin(
            "ErrorResponse",
            {
                "error": {
                    "code": code,
                    "type": kind,
                    "message": default_message if message is None else message,
                    "param": None,
                    "request_id": request_id,
                    "retryable": should_retry,
                }
            },
        )
    )
    validate_response(operation, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


def _receipt_wire(receipt: BindingLifecycleReceipt) -> dict[str, Any]:
    draft = BindingDraft(
        receipt.binding_id,
        receipt.revision,
        receipt.revision_id,
        receipt.definition,
        receipt.created_at_us,
    )
    published = PublishedBinding(
        published_at_us=receipt.published_at_us,
        capacity_manifest_id=receipt.capacity_manifest_id,
        qualification_report_id=receipt.qualification_report_id,
        status=receipt.status,
        retired_at_us=receipt.retired_at_us,
        disabled_reason=receipt.disabled_reason,
    )
    return revision_to_wire(draft, published)


def _intent(operation: str, envelope: dict[str, Any]) -> bytes:
    return encode_document(
        {
            "operation": operation,
            "path": envelope["path"],
            "body": envelope["body"],
        }
    )


def _command(
    operation: str,
    envelope: dict[str, Any],
    request_id: str,
    *,
    actor_type: str,
    credential_label: str | None,
    key: str | None,
) -> BindingPublicationCommand | BindingRetirementCommand | BindingActivationCommand:
    identity = envelope["path"]
    body = envelope["body"]
    intent = _intent(operation, envelope)
    # The publication path names only the binding; its body names the exact
    # draft revision, so the command revision comes from the validated body.
    revision = body["expected_revision"] if operation == PUBLISH else identity["revision"]
    common = {
        "binding_id": identity["binding_id"],
        "revision": revision,
        "request_id": request_id,
        "actor_type": actor_type,
        "credential_label": credential_label,
        "key_digest": None if key is None else document_digest(key.encode("utf-8")),
        "intent_digest": document_digest(intent),
    }
    if operation == RETIRE:
        return BindingRetirementCommand(
            **common,
            mode=body["mode"],
            reason=body.get("reason"),
        )
    if operation == PUBLISH:
        return BindingPublicationCommand(
            **common,
            expected_current_revision=body["expected_current_revision"],
            capacity_manifest_id=body["capacity_manifest_id"],
            qualification_report_id=body["qualification_report_id"],
        )
    return BindingActivationCommand(
        **common,
        expected_current_revision=body["expected_current_revision"],
    )


def _run(
    provider: BindingLifecycle,
    operation: str,
    envelope: dict[str, Any],
    request_id: str,
    *,
    actor_type: str,
    credential_label: str | None,
) -> Response:
    try:
        key = envelope["headers"].get("idempotency-key")
        command = _command(
            operation,
            envelope,
            request_id,
            actor_type=actor_type,
            credential_label=credential_label,
            key=key,
        )
        if operation == PUBLISH:
            receipt = provider.publish(cast(BindingPublicationCommand, command))
        elif operation == RETIRE:
            receipt = provider.retire(cast(BindingRetirementCommand, command))
        else:
            receipt = provider.activate(cast(BindingActivationCommand, command))
        wire = _receipt_wire(receipt)
        headers = {"Cache-Control": "no-store"}
        if operation == RETIRE and receipt.status == "disabled":
            headers["X-Binding-Disable-Cleanup"] = "pending_or_unknown"
        # A publication creates one fact; only the original key replays it.
        status = 200 if operation != PUBLISH or receipt.replayed else 201
        validate_response(operation, status, {"body": wire, "headers": headers})
        return JSONResponse(wire, status_code=status, headers=headers)
    except BindingLifecycleNotFoundError:
        return _error("binding_not_found", request_id, operation)
    except (BindingActivationConflictError, BindingPublicationHeadConflictError) as error:
        intent = (
            "publication"
            if isinstance(error, BindingPublicationHeadConflictError)
            else "activation"
        )
        return _error(
            error.code,
            request_id,
            operation,
            message=(
                "The active binding revision changed. "
                f"Expected expected_current_revision={json.dumps(error.current_revision)} "
                f"when this {intent} was rejected. "
                f"Refresh the binding state and confirm the intended {intent} before resubmitting."
            ),
        )
    except BindingPublicationEvidenceError:
        return _error("capacity_manifest_mismatch", request_id, operation)
    except BindingLifecycleConflictError as error:
        return _error(error.code, request_id, operation)
    except BindingLifecycleUnavailableError:
        return _error("binding_result_unknown", request_id, operation, retryable=False)
    except Exception:
        return _error("binding_result_unknown", request_id, operation, retryable=False)


def create_binding_lifecycle_router(
    get_provider: Callable[[], BindingLifecycle | None],
) -> APIRouter:
    router = APIRouter(tags=["model-bindings"])

    async def handle(request: Request, *, operation: str) -> Response:
        request_id = str(request.state.request_id)
        provider = get_provider()
        if provider is None:
            return _error("binding_result_unknown", request_id, operation, retryable=False)
        try:
            pairs = list(request.query_params.multi_items())
            if pairs:
                raise ContractValidationError("Lifecycle commands have no query")
            headers: dict[str, str] = {}
            for name in ("x-request-id", "idempotency-key"):
                values = request.headers.getlist(name)
                if len(values) > 1:
                    raise ContractValidationError("Ambiguous header")
                if values:
                    headers[name] = values[0]
            body = _json(await request.body())
            envelope = {"path": request.path_params, "headers": headers, "body": body}
            validate_request(operation, envelope)
            authorization = request.headers.getlist("authorization")
            if len(authorization) > 1:
                raise ContractValidationError("Ambiguous authorization")
            # A client-supplied header is not evidence of an authenticated identity.
            return await run_in_threadpool(
                _run,
                provider,
                operation,
                envelope,
                request_id,
                actor_type="anonymous",
                credential_label=None,
            )
        except ContractValidationError, ValueError, TypeError, KeyError, RecursionError:
            return _error("invalid_binding_request", request_id, operation)

    async def activate(request: Request) -> Response:
        return await handle(request, operation=ACTIVATE)

    async def retire(request: Request) -> Response:
        return await handle(request, operation=RETIRE)

    async def publish(request: Request) -> Response:
        return await handle(request, operation=PUBLISH)

    router.add_api_route(ACTIVATE_PATH, activate, methods=["POST"], operation_id=ACTIVATE)
    router.add_api_route(RETIRE_PATH, retire, methods=["POST"], operation_id=RETIRE)
    router.add_api_route(PUBLISH_PATH, publish, methods=["POST"], operation_id=PUBLISH)
    return router
