"""Generated management HTTP boundaries with injected services and deletion views."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import speechnexus_contracts.workbench_models as wire
from fastapi import APIRouter, Request
from speechnexus_contracts.workbench_http import contract, validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.workbench.application.deletions import WorkbenchDeletions
from speechnexus_backend.workbench.application.meetings import WorkbenchMeetings
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.deletion import (
    ConfirmationMismatchError,
    DeletionDependencyConflictError,
    DeletionOwnerMismatchError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionPlanExpiredError,
    DeletionRecord,
    DeletionTarget,
)
from speechnexus_backend.workbench.interfaces.cursors import CursorCodec, InvalidCursorError
from speechnexus_backend.workbench.interfaces.http_ingress import (
    IngressError,
    default_limit,
    read_envelope,
    request_id,
)
from speechnexus_backend.workbench.interfaces.http_versions import _error
from speechnexus_backend.workbench.interfaces.version_views import (
    project_payload,
    transcript_payload,
)

OPERATIONS = frozenset(
    {
        "getWorkbenchStatus",
        "listProjects",
        "listTranscripts",
        "workbenchCreateDeletionPlan",
        "workbenchCreateDeletion",
        "workbenchGetDeletion",
    }
)


@dataclass(frozen=True, slots=True)
class DeletionResponses:
    plan: Callable[[DeletionPlan], dict[str, object]]
    accepted: Callable[[DeletionRecord], dict[str, object]]
    current: Callable[[DeletionRecord], dict[str, object]]


def _default_limit(document: dict[str, Any], operation: dict[str, Any]) -> int:
    return default_limit(document, operation)


def _target(value: Any) -> DeletionTarget:
    return DeletionTarget(
        value["owner"], value["kind"], value["resource"]["id"], int(value["resource"]["revision"])
    )


def create_management_router(
    versions: WorkbenchVersions | None,
    deletions: WorkbenchDeletions | None,
    *,
    default_workspace_id: str,
    cursor: CursorCodec,
    responses: DeletionResponses,
    max_json_bytes: int,
    body_timeout_seconds: float,
    meetings: WorkbenchMeetings | None = None,
    capabilities: Mapping[str, str] | None = None,
) -> APIRouter:
    router = APIRouter()
    document = contract()
    defaults = {
        op["operationId"]: _default_limit(document, op)
        for item in document["paths"].values()
        for op in item.values()
        if op["operationId"] in {"listProjects", "listTranscripts"}
    }

    async def dispatch(operation: str, value: dict[str, Any]) -> tuple[int, Any]:
        query = value["query"]
        workspace = query.get("workspace_id", default_workspace_id)
        if operation == "getWorkbenchStatus":
            profile = None if meetings is None else meetings.llm_profile
            return 200, {
                "object": "nexus.workbench_status",
                "workbench_enabled": versions is not None
                or deletions is not None
                or meetings is not None,
                "llm_enabled": meetings is not None and meetings.llm_enabled,
                "shared_deployment_notice": "shared_deployment",
                # Deployment-derived states, in the contract's canonical order. Consumers
                # read these instead of inferring readiness from configuration they cannot
                # see, and must not fill a missing capability in themselves.
                "capabilities": [
                    {"name": name, "status": status}
                    for name, status in (capabilities or {}).items()
                ],
                **(
                    {
                        "minutes_profile": {
                            "template_revision": str(profile.template_revision),
                            "llm_model": profile.model,
                        }
                    }
                    if profile is not None
                    else {}
                ),
            }
        if operation == "listProjects":
            if versions is None:
                raise IngressError("workbench_unavailable")
            after = (
                cursor.decode(query["cursor"], kind="projects", parent_id=workspace)
                if "cursor" in query
                else None
            )
            limit = int(query["limit"]) if "limit" in query else defaults[operation]
            projects = await versions.list_projects(
                workspace_id=workspace, limit=limit, after=after
            )
            data: list[wire.Project | wire.Transcript] = [
                project_payload(p) for p in projects.items
            ]
            position = projects.next_position
        elif operation == "listTranscripts":
            if versions is None:
                raise IngressError("workbench_unavailable")
            parent = value["path"]["project_id"]
            after = (
                cursor.decode(query["cursor"], kind="transcripts", parent_id=parent)
                if "cursor" in query
                else None
            )
            limit = int(query["limit"]) if "limit" in query else defaults[operation]
            transcripts = await versions.list_transcripts(
                project_id=parent, limit=limit, after=after
            )
            data = [transcript_payload(t) for t in transcripts.items]
            position = transcripts.next_position
        else:
            if deletions is None:
                raise IngressError("deletions_unavailable")
            if operation == "workbenchGetDeletion":
                return 200, responses.current(
                    await deletions.get(workspace, value["path"]["deletion_id"])
                )
            key = value["headers"]["idempotency-key"]
            body = value["body"]
            if operation == "workbenchCreateDeletionPlan":
                target = _target(body["target"])
                if target.owner != "workbench":
                    raise DeletionOwnerMismatchError
                if target.kind not in {"project", "transcript"}:
                    raise IngressError("deletion_resource_unsupported")
                plan = await deletions.preview(
                    workspace_id=workspace, target=target, mode=body["mode"], key=key
                )
                return 200, responses.plan(plan)
            record = await deletions.accept(
                workspace_id=workspace,
                plan_id=body["plan_id"],
                expected_revision=int(body["expected_plan_revision"]),
                digest=body["plan_digest"],
                targets=tuple(_target(t) for t in body["confirm_targets"]),
                key=key,
            )
            return 202, responses.accepted(record)
        next_cursor = None if position is None else cursor.encode(position)
        return 200, {
            "object": "nexus.list",
            "data": data,
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
        }

    def endpoint(operation: str, has_body: bool) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            rid = request_id(request)
            try:
                envelope = await read_envelope(
                    request,
                    operation,
                    has_body=has_body,
                    max_json_bytes=max_json_bytes,
                    body_timeout_seconds=body_timeout_seconds,
                )
                status, body = await dispatch(operation, envelope)
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
            except DeletionOwnerMismatchError:
                return _error("deletion_owner_mismatch", rid, operation)
            except ConfirmationMismatchError:
                return _error("deletion_confirmation_mismatch", rid, operation)
            except DeletionPlanExpiredError:
                return _error("deletion_plan_expired", rid, operation)
            except DeletionPlanChangedError:
                return _error("deletion_plan_changed", rid, operation)
            except DeletionDependencyConflictError:
                return _error("deletion_dependency_conflict", rid, operation)
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
        for method, op in item.items():
            name = op["operationId"]
            if name in OPERATIONS:
                router.add_api_route(
                    path,
                    endpoint(name, "requestBody" in op),
                    methods=[method.upper()],
                    operation_id=name,
                    response_model=None,
                )
    return router
