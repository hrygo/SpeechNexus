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
from collections.abc import Awaitable, Callable, Iterable, Iterator
from functools import cache
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from speechnexus_backend.speech.interfaces.http.unimplemented_errors import (
    OPERATION_NOT_IMPLEMENTED,
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


@cache
def _path_pattern(path: str) -> re.Pattern[str]:
    segments = [
        r"[^/]+" if segment.startswith("{") and segment.endswith("}") else re.escape(segment)
        for segment in path.split("/")
    ]
    return re.compile("/".join(segments) + r"\Z")


_PATH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    _path_pattern(path) for path in sorted({path for _, path in UNIMPLEMENTED_OPERATIONS.values()})
)

# Neither source is trusted to agree on implicit methods: the pinned Starlette
# does not add HEAD to a GET route, another may, and the framework's own `Allow`
# never advertises HEAD or OPTIONS anyway. Both are dropped from the merged
# header so it stays deterministic across router versions.
IMPLICIT_METHODS = frozenset({"HEAD", "OPTIONS"})


def is_unimplemented_path(path: str) -> bool:
    """True when a registered placeholder owns this concrete request path."""
    return any(pattern.fullmatch(path) for pattern in _PATH_PATTERNS)


def matches_template(template: str, path: str) -> bool:
    """True when a concrete request path fills this route template."""
    return _path_pattern(template).fullmatch(path) is not None


def iter_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Flatten routers this FastAPI keeps behind a lazy include wrapper."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from iter_routes(inner.routes)
        else:
            yield route


def methods_for_path(routes: Iterable[Any], path: str) -> frozenset[str]:
    """Every method registered on the template this concrete path fills.

    RFC 9110 requires a 405 `Allow` header to list all methods the resource
    supports; the framework only reports the first partially matching route.
    """
    methods: set[str] = set()
    for route in iter_routes(routes):
        template = getattr(route, "path", None)
        if not isinstance(template, str) or not matches_template(template, path):
            continue
        methods.update(getattr(route, "methods", None) or ())
    return frozenset(method for method in methods if method not in IMPLICIT_METHODS)


def assert_placeholder_ownership(routes: Iterable[Any]) -> None:
    """Fail fast when a real route shares a placeholder's method and template.

    Registration order decides which route wins, so a feature router included
    after the placeholders would answer 503 for an operation that ships.
    """
    owners: dict[tuple[str, str], list[str]] = {}
    for route in iter_routes(routes):
        template = getattr(route, "path", None)
        if not isinstance(template, str):
            continue
        module = getattr(getattr(route, "endpoint", None), "__module__", "") or ""
        for method in getattr(route, "methods", None) or ():
            owners.setdefault((method, template), []).append(module)
    collisions = [
        f"{method} {path}"
        for method, path in sorted(UNIMPLEMENTED_OPERATIONS.values())
        if owners.get((method, path), []) != [__name__]
    ]
    if collisions:
        raise RuntimeError(
            "placeholder routes must be the only owner of their method and template; "
            "include the placeholder router last: " + ", ".join(collisions)
        )


def _handler(operation_id: str) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def placeholder(request: Request) -> JSONResponse:
        # The single-entry invariant of the error map is enforced statically by
        # the route conformance gate (`unimplemented_error_map_drift`).
        return operation_not_implemented_error(
            OPERATION_NOT_IMPLEMENTED, str(request.state.request_id)
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


__all__ = [
    "IMPLICIT_METHODS",
    "UNIMPLEMENTED_OPERATIONS",
    "assert_placeholder_ownership",
    "create_unimplemented_router",
    "is_unimplemented_path",
    "methods_for_path",
]
