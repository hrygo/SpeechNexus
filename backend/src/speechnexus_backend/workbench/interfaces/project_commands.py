"""Generated project-body validation and mapping into workbench use cases.

This process-local adapter returns domain snapshots, not public response DTOs.
A transport must still validate headers/path parameters and map responses/errors.
The composition root supplies the deployment's default workspace; this adapter
does not create workspaces, enforce their lifecycle, or establish an identity.
"""

from __future__ import annotations

from typing import Any

from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from speechnexus_backend.workbench.domain.versions import Project, ProjectChanges, Unset


class ProjectCommandBoundary:
    """Consume A's generated body contract before any transaction is opened."""

    def __init__(self, service: WorkbenchVersions, *, default_workspace_id: str) -> None:
        self._service = service
        self._default_workspace_id = default_workspace_id

    async def create_project(self, request: Any, *, idempotency_key: str | None = None) -> Project:
        body = parse("ProjectCreateRequest", request)
        return await self._service.create_project(
            workspace_id=body.get("workspace_id", self._default_workspace_id),
            name=body["name"],
            description=body.get("description"),
            idempotency_key=idempotency_key,
        )

    async def update_project(
        self, project_id: str, request: Any, *, idempotency_key: str | None = None
    ) -> Project:
        body = parse("ProjectPatch", request)
        return await self._service.edit_project(
            project_id=project_id,
            # JSON Schema integer also admits an integral JSON number (e.g. 1.0).
            # Normalize its domain representation before computing the intent digest.
            expected_revision=int(body["expected_metadata_revision"]),
            changes=ProjectChanges(
                name=body.get("name", Unset.VALUE),
                description=body.get("description", Unset.VALUE),
                status=body.get("status", Unset.VALUE),
            ),
            idempotency_key=idempotency_key,
        )
