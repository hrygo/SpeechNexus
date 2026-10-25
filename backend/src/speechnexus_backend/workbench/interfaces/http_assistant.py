"""Three generated HTTP operations over the private assistant snapshot.

The assistant is off unless a server-side intranet LLM is configured, so the
default deployment answers creation with a stable 503 and stores nothing.
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.assistant import WorkbenchAssistant
from speechnexus_backend.workbench.application.llm import LlmError
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.assistant import AssistantStateConflictError
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.interfaces.assistant_views import assistant_turn_payload
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    read_envelope,
    request_id,
    validate_limits,
)
from speechnexus_backend.workbench.interfaces.http_versions import _error

OPERATIONS = frozenset({"createAssistantTurn", "getAssistantTurn", "cancelAssistantTurn"})

# Stable text-LLM codes from the shared error table; anything unrecognised reports
# as unavailable rather than inventing a new public code.
_LLM_CODES = {
    "disabled": "llm_disabled",
    "invalid_input": "llm_invalid_input",
    "profile_mismatch": "llm_profile_mismatch",
    "unavailable": "llm_unavailable",
    "invalid_response": "llm_invalid_response",
    "source_not_sealed": "llm_source_not_sealed",
    "source_changed": "llm_source_changed",
    "source_unavailable": "llm_source_unavailable",
}


def create_assistant_router(
    service: WorkbenchAssistant | None,
    *,
    default_workspace_id: str,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> APIRouter:
    validate_limits(max_json_bytes, body_timeout_seconds)
    parse(
        "AssistantTurnCreateRequest",
        cast(Any, {"input_text": "synthetic", "voice": {"id": "voice_demo", "revision": "1"}}),
    )
    document = contract()
    router = APIRouter()

    async def dispatch(operation: str, value: dict[str, Any]) -> tuple[int, Any]:
        if service is None:
            raise IngressError("workbench_unavailable")
        path = value["path"]
        if operation == "getAssistantTurn":
            return 200, assistant_turn_payload(
                await service.get_turn(turn_id=path["turn_id"], workspace_id=default_workspace_id)
            )
        if operation == "cancelAssistantTurn":
            turn, changed = await service.cancel_turn(
                turn_id=path["turn_id"], workspace_id=default_workspace_id
            )
            # 200 means this call cancelled a live turn; an already finished turn is
            # accepted without work, which keeps repeated cancellation idempotent.
            return (200 if changed else 202), assistant_turn_payload(turn)
        body = value["body"]
        voice = body["voice"]
        profile = body.get("profile")
        return 202, assistant_turn_payload(
            await service.create_turn(
                workspace_id=default_workspace_id,
                project_id=path["project_id"],
                input_text=body["input_text"],
                voice_id=voice["id"],
                voice_revision=int(voice["revision"]),
                model=body.get("model"),
                profile_id=None if profile is None else profile["id"],
                profile_revision=None if profile is None else int(profile["revision"]),
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
            except LlmError as error:
                return _error(_LLM_CODES.get(error.code, "llm_unavailable"), rid, operation)
            except ResourceNotFoundError:
                return _error("resource_not_found", rid, operation)
            except IdempotencyConflictError:
                return _error("idempotency_conflict", rid, operation)
            except RevisionConflictError:
                return _error("revision_conflict", rid, operation)
            except AssistantStateConflictError:
                return _error("invalid_change", rid, operation)
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
