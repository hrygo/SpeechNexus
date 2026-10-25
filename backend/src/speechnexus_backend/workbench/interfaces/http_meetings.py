"""Five generated HTTP operations over atomic, injected meeting commands."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts.workbench import parse
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.llm import LlmError
from speechnexus_backend.workbench.application.meetings import WorkbenchMeetings
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.meetings import MeetingSourceUnsupportedError
from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTaskConflictError
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
    Unset,
)
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    read_envelope,
    request_id,
    validate_limits,
)
from speechnexus_backend.workbench.interfaces.http_versions import _error
from speechnexus_backend.workbench.interfaces.meeting_views import meeting_payload, minutes_payload

OPERATIONS = frozenset(
    {"createMeeting", "getMeeting", "sealMeeting", "createMeetingMinutes", "getMeetingMinutes"}
)


def create_meeting_router(
    service: WorkbenchMeetings | None,
    *,
    default_workspace_id: str,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> APIRouter:
    validate_limits(max_json_bytes, body_timeout_seconds)
    parse(
        "ProjectCreateRequest",
        {"name": "configuration-check", "workspace_id": default_workspace_id},
    )
    document = contract()
    save_audio_default = document["components"]["schemas"]["MeetingCreateRequest"]["properties"][
        "save_audio"
    ]["default"]
    if type(save_audio_default) is not bool:
        raise ValueError("invalid generated meeting audio default")
    router = APIRouter()

    async def dispatch(operation: str, value: dict[str, Any]) -> tuple[int, Any]:
        if service is None:
            raise IngressError("meetings_unavailable")
        path = value["path"]
        key = value["headers"].get("idempotency-key")
        if operation == "createMeeting":
            body = value["body"]
            view = await service.create(
                project_id=path["project_id"],
                workspace_id=default_workspace_id,
                transcript_id=body.get("transcript_id"),
                speech_session_id=body.get("speech_session_id"),
                display_name=body["display_name"],
                save_audio=body.get("save_audio", save_audio_default),
                key=key,
            )
            return 201, meeting_payload(view)
        meeting_id = path["meeting_id"]
        if operation == "getMeeting":
            return 200, meeting_payload(
                await service.get(meeting_id=meeting_id, workspace_id=default_workspace_id)
            )
        if operation == "sealMeeting":
            body = value["body"]
            view = await service.seal(
                meeting_id=meeting_id,
                workspace_id=default_workspace_id,
                expected_revision=int(body["expected_transcript_revision"]),
                text=body.get("text", Unset.VALUE),
                key=key,
            )
            return 200, meeting_payload(view)
        if operation == "createMeetingMinutes":
            body = value["body"]
            job = await service.create_minutes(
                meeting_id=meeting_id,
                workspace_id=default_workspace_id,
                template_revision=int(body["template_revision"]),
                llm_model=body.get("llm_model"),
                key=key,
                expected_source_revision=int(body["expected_source_revision"])
                if "expected_source_revision" in body
                else None,
            )
            return 202, minutes_payload(meeting_id, job)
        job = await service.get_minutes(
            meeting_id=meeting_id, workspace_id=default_workspace_id, minutes_id=path["minutes_id"]
        )
        return 200, minutes_payload(meeting_id, job)

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
            except ResourceNotFoundError:
                return _error("resource_not_found", rid, operation)
            except IdempotencyConflictError:
                return _error("idempotency_conflict", rid, operation)
            except RevisionConflictError:
                return _error("revision_conflict", rid, operation)
            except InvalidVersionChangeError:
                return _error("invalid_change", rid, operation)
            except MeetingSourceUnsupportedError:
                return _error("meeting_source_unsupported", rid, operation)
            except MinutesTaskConflictError:
                return _error("minutes_conflict", rid, operation)
            except LlmError as error:
                return _error("llm_" + error.code, rid, operation)
            except VersionStoreError as error:
                return _error(
                    "outcome_unknown" if error.commit_unknown else "store_unavailable",
                    rid,
                    operation,
                )
            except Exception:
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
