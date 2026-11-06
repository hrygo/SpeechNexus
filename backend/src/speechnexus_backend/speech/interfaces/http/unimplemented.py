"""Central placeholders for contract operations this deployment does not implement.

Without a registered route an unimplemented operation falls through to the
framework: a bare 404 whose body is not the public error envelope, a 405 from a
path that accepts another method, or an ingress-specific 400. Each of those
reads to a consumer as "the resource does not exist", "the method is wrong" or
"the request is invalid", which is exactly the confusion #473 removes.

Every unimplemented operation is therefore registered here once and answers 503
``operation_not_implemented``. The table is mirrored by
``KNOWN_UNIMPLEMENTED_OPERATIONS`` in
``scripts/dev-env/check-route-conformance.py``; the gate fails when the two
drift, so implementing an operation cannot leave a silent placeholder behind.
Routes stay out of the served OpenAPI document: a deployment must not advertise
an operation it cannot perform.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from speechnexus_backend.speech.interfaces.http.unimplemented_errors import (
    operation_not_implemented_error,
)

# operationId -> (method, path). Keep aligned with the source contract.
UNIMPLEMENTED_OPERATIONS: dict[str, tuple[str, str]] = {
    "retireVoiceRevision": (
        "POST",
        "/v1/nexus/voices/{voice_id}/revisions/{revision}/retirements",
    ),
    "listPronunciationSets": ("GET", "/v1/nexus/pronunciation-sets"),
    "createPronunciationRevision": ("POST", "/v1/nexus/pronunciation-sets"),
    "listJobs": ("GET", "/v1/nexus/jobs"),
    "getOperation": ("GET", "/v1/nexus/operations/{operation_id}"),
    "cancelOperation": ("POST", "/v1/nexus/operations/{operation_id}/cancellations"),
    "createRealtimeSession": ("POST", "/v1/nexus/realtime/sessions"),
    "getRealtimeSession": ("GET", "/v1/nexus/realtime/sessions/{session_id}"),
    "cancelRealtimeSession": (
        "POST",
        "/v1/nexus/realtime/sessions/{session_id}/cancellations",
    ),
    "connectRealtime": ("GET", "/v1/realtime"),
    "createVoiceDesign": ("POST", "/v1/nexus/voice-designs"),
    "createVoiceClone": ("POST", "/v1/nexus/voice-clones"),
    "getVoiceDraft": ("GET", "/v1/nexus/voice-drafts/{draft_id}"),
    "updateVoiceDraftMetadata": ("PATCH", "/v1/nexus/voice-drafts/{draft_id}"),
    "generateVoiceDraftRevision": (
        "POST",
        "/v1/nexus/voice-drafts/{draft_id}/generations",
    ),
    "createVoicePreview": ("POST", "/v1/nexus/voice-drafts/{draft_id}/previews"),
    "validateVoiceDraftRevision": (
        "POST",
        "/v1/nexus/voice-drafts/{draft_id}/validations",
    ),
    "publishVoiceDraftRevision": (
        "POST",
        "/v1/nexus/voice-drafts/{draft_id}/publications",
    ),
    "speechCreateDeletionPlan": ("POST", "/v1/nexus/deletion-plans"),
    "speechCreateDeletion": ("POST", "/v1/nexus/deletions"),
    "speechGetDeletion": ("GET", "/v1/nexus/deletions/{deletion_id}"),
}


def _path_pattern(path: str) -> re.Pattern[str]:
    segments = [
        r"[^/]+" if segment.startswith("{") and segment.endswith("}") else re.escape(segment)
        for segment in path.split("/")
    ]
    return re.compile("/".join(segments) + r"\Z")


_PATH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    _path_pattern(path) for path in sorted({path for _, path in UNIMPLEMENTED_OPERATIONS.values()})
)


def is_unimplemented_path(path: str) -> bool:
    """True when a registered placeholder owns this concrete request path."""
    return any(pattern.fullmatch(path) for pattern in _PATH_PATTERNS)


def _handler(operation_id: str) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def placeholder(request: Request) -> JSONResponse:
        return operation_not_implemented_error(
            "operation_not_implemented", str(request.state.request_id)
        )

    placeholder.__name__ = f"unimplemented_{operation_id}"
    return placeholder


def create_unimplemented_router() -> APIRouter:
    router = APIRouter(tags=["unimplemented"])
    for operation_id, (method, path) in sorted(UNIMPLEMENTED_OPERATIONS.items()):
        router.add_api_route(
            path,
            _handler(operation_id),
            methods=[method],
            operation_id=operation_id,
            include_in_schema=False,
        )
    return router


__all__ = ["UNIMPLEMENTED_OPERATIONS", "create_unimplemented_router", "is_unimplemented_path"]
