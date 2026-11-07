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

The ownership guard compares the resource Starlette actually serves, not the
literal template spelling. Starlette's ``redirect_slashes`` answers a
slash-suffixed path with a 307 to the path stripped of *every* trailing slash
and re-matches, so ``/v1/nexus/voice-drafts/{draft_id}/`` and the placeholder
without the slash own the same requests. The comparison therefore folds both
parameter spelling (name and converter) and that trailing-slash alias, while the
message still reports the template the placeholder table declares.

The guard only sees the assembled route table, so it covers rewrites the
application itself performs. Measured against the pinned framework, the
following are *not* aliases of a registered template and stay distinct:

* repeated slashes inside a path and a leading ``//``: no route matches and no
  redirect is issued (404);
* percent-encoded separators: ASGI servers decode before routing, so
  ``abc%2Fdef`` becomes an extra path segment (404), while ``%20`` decodes to a
  character the parameter already matches;
* path case: matching is case-sensitive (``/VOICE-DRAFTS/abc`` is 404);
* ``.`` and ``..`` segments: the application does not resolve them (404), but
  HTTP clients and reverse proxies may normalise them before the request
  arrives; such an ingress rewrite happens before ASGI routing and is outside
  what an in-process route guard can observe.

Ownership is method-specific: normalization folds only a method+template pair
that the placeholder table owns. For example, the voice-draft placeholders own
GET and PATCH, but not DELETE; a real DELETE route at the slash-suffixed
template may coexist, even though the same slash is an alias for GET/PATCH.
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
    "getOperation": ("GET", "/v1/nexus/operations/{operation_id}"),
    "cancelOperation": ("POST", "/v1/nexus/operations/{operation_id}/cancellations"),
    "createRealtimeSession": ("POST", "/v1/nexus/realtime/sessions"),
    "getRealtimeSession": ("GET", "/v1/nexus/realtime/sessions/{session_id}"),
    "cancelRealtimeSession": (
        "POST",
        "/v1/nexus/realtime/sessions/{session_id}/cancellations",
    ),
    "connectRealtime": ("GET", "/v1/realtime"),
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

# Neither source is trusted to agree on implicit methods: Starlette's plain
# `Route(methods=["GET"])` adds HEAD on its own while FastAPI's `APIRoute` does
# not, and a raw Starlette route may also declare OPTIONS explicitly. Both are
# dropped from the merged header so it stays deterministic across router
# versions.
IMPLICIT_METHODS = frozenset({"HEAD", "OPTIONS"})

# A template parameter, including its optional converter: {draft_id},
# {draft_id:int}, {artifact_path:path}.
_TEMPLATE_PARAMETER = re.compile(r"\{[^/{}]*\}")


def normalize_template(template: str) -> str:
    """Compare the resource Starlette serves, not the literal template spelling.

    `/v1/nexus/voice-drafts/{draft_id}`, `.../{voice_draft_id}` and
    `.../{draft_id:int}` fill the same request paths, so a real route using any
    of them competes with the placeholder for the same requests even though the
    literal spellings differ.

    Trailing slashes name the same resource too: Starlette's
    `redirect_slashes` answers `.../{draft_id}/` (and `...//`) with a 307 to
    `.../{draft_id}` before re-matching, and strips *every* trailing slash.
    Folding them catches a feature router mounted only at the slash-suffixed
    spelling. Owners are therefore compared on this normalized shape; the
    message keeps the template the placeholder table declares.

    A template made only of slashes is kept intact: Starlette does not redirect
    ``//`` to ``/``, so stripping every slash would conflate distinct paths.
    """
    shape = _TEMPLATE_PARAMETER.sub("{}", template)
    return shape.rstrip("/") or shape


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
    """Fail when a real route shares a placeholder-owned method and template.

    Registration order decides which route wins, so a feature router included
    after the placeholders would answer 503 for an operation that ships. The
    comparison uses the normalized template, so renaming a parameter, adding a
    converter or appending trailing slashes cannot hide the conflict. It only
    checks method+template pairs declared by the placeholder table; another
    method at the same template remains available for a real route.
    """
    owners: dict[tuple[str, str], list[str]] = {}
    for route in iter_routes(routes):
        template = getattr(route, "path", None)
        if not isinstance(template, str):
            continue
        module = getattr(getattr(route, "endpoint", None), "__module__", "") or ""
        shape = normalize_template(template)
        for method in getattr(route, "methods", None) or ():
            owners.setdefault((method, shape), []).append(module)
    collisions = [
        f"{method} {path}"
        for method, path in sorted(UNIMPLEMENTED_OPERATIONS.values())
        if owners.get((method, normalize_template(path)), []) != [__name__]
    ]
    if collisions:
        raise RuntimeError(
            "real routes must not share a method+template with a placeholder; "
            "placeholders must be the only owner: " + ", ".join(collisions)
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
    "normalize_template",
]
