"""C-owned composition factory; production root and migrations remain explicit."""

from collections.abc import Mapping

from fastapi import FastAPI
from starlette.types import Lifespan

from speechnexus_backend.workbench.application.assistant import WorkbenchAssistant
from speechnexus_backend.workbench.application.books import WorkbenchBooks
from speechnexus_backend.workbench.application.deletions import WorkbenchDeletions
from speechnexus_backend.workbench.application.exports import WorkbenchExports
from speechnexus_backend.workbench.application.meetings import WorkbenchMeetings
from speechnexus_backend.workbench.application.render_units import WorkbenchRenderUnits
from speechnexus_backend.workbench.application.saved_assets import WorkbenchSavedAssets
from speechnexus_backend.workbench.application.teleprompter import WorkbenchTeleprompter
from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from speechnexus_backend.workbench.application.voices import WorkbenchVoices
from speechnexus_backend.workbench.infrastructure.deletion_codec import (
    accepted_payload,
    deletion_payload,
    plan_payload,
)
from speechnexus_backend.workbench.interfaces.cursors import CursorCodec
from speechnexus_backend.workbench.interfaces.http_assistant import (
    create_assistant_router,
)
from speechnexus_backend.workbench.interfaces.http_books import create_book_router
from speechnexus_backend.workbench.interfaces.http_exports import (
    create_export_router,
)
from speechnexus_backend.workbench.interfaces.http_management import (
    DeletionResponses,
    create_management_router,
)
from speechnexus_backend.workbench.interfaces.http_meetings import create_meeting_router
from speechnexus_backend.workbench.interfaces.http_render_units import (
    create_render_unit_router,
)
from speechnexus_backend.workbench.interfaces.http_saved_assets import (
    create_saved_asset_router,
)
from speechnexus_backend.workbench.interfaces.http_teleprompter import (
    create_teleprompter_router,
)
from speechnexus_backend.workbench.interfaces.http_versions import create_workbench_app
from speechnexus_backend.workbench.interfaces.http_voices import create_voice_router


def create_app(
    versions: WorkbenchVersions | None,
    deletions: WorkbenchDeletions | None = None,
    *,
    default_workspace_id: str,
    deployment_id: str,
    cursor_key: bytes,
    max_json_bytes: int,
    body_timeout_seconds: float,
    meetings: WorkbenchMeetings | None = None,
    teleprompter: WorkbenchTeleprompter | None = None,
    saved_assets: WorkbenchSavedAssets | None = None,
    books: WorkbenchBooks | None = None,
    render_units: WorkbenchRenderUnits | None = None,
    assistant: WorkbenchAssistant | None = None,
    exports: WorkbenchExports | None = None,
    voices: WorkbenchVoices | None = None,
    capabilities: Mapping[str, str] | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    app = create_workbench_app(
        versions,
        default_workspace_id=default_workspace_id,
        max_json_bytes=max_json_bytes,
        body_timeout_seconds=body_timeout_seconds,
        lifespan=lifespan,
    )
    cursor_codec = CursorCodec(deployment_id, cursor_key)
    app.include_router(
        create_management_router(
            versions,
            deletions,
            default_workspace_id=default_workspace_id,
            cursor=cursor_codec,
            responses=DeletionResponses(plan_payload, accepted_payload, deletion_payload),
            max_json_bytes=max_json_bytes,
            body_timeout_seconds=body_timeout_seconds,
            meetings=meetings,
            capabilities=capabilities,
        )
    )
    if meetings is not None:
        app.include_router(
            create_meeting_router(
                meetings,
                default_workspace_id=default_workspace_id,
                max_json_bytes=max_json_bytes,
                body_timeout_seconds=body_timeout_seconds,
            )
        )
    if teleprompter is not None:
        app.include_router(
            create_teleprompter_router(
                teleprompter,
                default_workspace_id=default_workspace_id,
                max_json_bytes=max_json_bytes,
                body_timeout_seconds=body_timeout_seconds,
            )
        )
    if books is not None:
        app.include_router(
            create_book_router(
                books,
                default_workspace_id=default_workspace_id,
                cursor=cursor_codec,
                max_json_bytes=max_json_bytes,
                body_timeout_seconds=body_timeout_seconds,
            )
        )
    if render_units is not None:
        app.include_router(
            create_render_unit_router(
                render_units,
                default_workspace_id=default_workspace_id,
                cursor=cursor_codec,
                max_json_bytes=max_json_bytes,
                body_timeout_seconds=body_timeout_seconds,
            )
        )
    if assistant is not None:
        app.include_router(
            create_assistant_router(
                assistant,
                default_workspace_id=default_workspace_id,
                max_json_bytes=max_json_bytes,
                body_timeout_seconds=body_timeout_seconds,
            )
        )
    if exports is not None:
        app.include_router(
            create_export_router(
                exports,
                default_workspace_id=default_workspace_id,
                max_json_bytes=max_json_bytes,
                body_timeout_seconds=body_timeout_seconds,
            )
        )
    if saved_assets is not None:
        app.include_router(
            create_saved_asset_router(
                saved_assets,
                default_workspace_id=default_workspace_id,
                max_json_bytes=max_json_bytes,
                body_timeout_seconds=body_timeout_seconds,
            )
        )
    # The catalogue routes stay mounted without one: the frozen operations must answer a
    # stable unavailable error rather than vanish when no speech base is configured.
    app.include_router(
        create_voice_router(
            voices,
            default_workspace_id=default_workspace_id,
            max_json_bytes=max_json_bytes,
            body_timeout_seconds=body_timeout_seconds,
        )
    )
    return app
