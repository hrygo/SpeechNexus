"""Published reads and explicit original-key lifecycle POSTs; one call, no retries."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.profile_lifecycle_http import validate_request as lifecycle_request
from speechnexus_contracts.profile_lifecycle_http import validate_response as lifecycle_response
from speechnexus_contracts.profile_public_http import validate_request as public_request
from speechnexus_contracts.profile_public_http import validate_response as public_response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.profile_drafts import ProfileDrafts
from speechnexus_backend.speech.application.profile_publications import ProfilePublications
from speechnexus_backend.speech.interfaces.http.profile_errors import profile_error
from speechnexus_backend.speech.interfaces.profile_lifecycle import (
    prepare_profile_activation,
    prepare_profile_publication,
)
from speechnexus_backend.speech.interfaces.profile_publications import publication_to_wire

PUBLIC_PATH = "/v1/nexus/profiles"
FIXED_PATH = PUBLIC_PATH + "/{profile_id}/revisions/{revision}"
ADMIN_REVISION = "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}"
PUBLISH = "publishProfileRevision"
ACTIVATE = "activatePublishedProfileRevision"
LIST = "listProfiles"
PUBLIC_READ = "getProfileRevision"


def _decode_cursor(value: str) -> tuple[str, str]:
    decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode())
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or any(not isinstance(v, str) for v in decoded)
    ):
        raise ValueError("Invalid cursor")
    return decoded[0], decoded[1]


def _encode_cursor(identity: str, revision: str) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps([identity, revision], separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _execute(
    provider: ProfilePublications, operation: str, envelope: dict[str, Any], request_id: str
) -> Response:
    def error(code: str) -> JSONResponse:
        return profile_error(code, request_id, operation=operation)

    mutation = operation in (PUBLISH, ACTIVATE)
    try:
        (lifecycle_request if mutation else public_request)(operation, envelope)
        if mutation:
            path = envelope["path"]
            prepare = (
                prepare_profile_publication if operation == PUBLISH else prepare_profile_activation
            )
            command = prepare(
                path["profile_id"],
                path["revision"],
                envelope["body"],
                envelope["headers"].get("idempotency-key"),
            )
        elif operation == LIST:
            query = envelope["query"]
            limit = int(query.get("limit", "50"))
            after = _decode_cursor(query["cursor"]) if "cursor" in query else None
            if after is not None:
                from speechnexus_backend.speech.domain.assets import identifier
                from speechnexus_backend.speech.domain.configuration import revision_number

                identifier(after[0])
                revision_number(after[1])
    except ContractValidationError, ValueError, TypeError, KeyError, RecursionError:
        return error("invalid_profile_request")
    try:
        if mutation:
            result = provider.mutate(command)
            if result.outcome in ("conflict", "rejected"):
                return error(result.error_code or "profile_result_unknown")
            if result.outcome not in ("completed", "replayed") or result.publication is None:
                return error("profile_result_unknown")
            if (result.publication.draft.profile_id, result.publication.draft.revision) != (
                command.profile_id,
                command.revision,
            ):
                return error("profile_result_unknown")
            wire = publication_to_wire(result.publication)
            lifecycle_response(operation, 200, {"body": wire})
        elif operation == PUBLIC_READ:
            path = envelope["path"]
            publication = provider.get_publication(path["profile_id"], path["revision"])
            if publication is None:
                return error(
                    "profile_not_published"
                    if provider.get(path["profile_id"], path["revision"]) is not None
                    else "profile_not_found"
                )
            if (publication.draft.profile_id, publication.draft.revision) != (
                path["profile_id"],
                path["revision"],
            ):
                return error("profile_unavailable")
            wire = publication_to_wire(publication)
            public_response(operation, 200, {"body": wire})
        else:
            page, more = provider.list_publications(limit=limit, after=after)
            data = []
            for publication, current in page:
                value = publication_to_wire(publication)
                data.append(
                    {
                        "id": value["profile_id"],
                        "revision": value["revision"],
                        "current_revision": current,
                        **{
                            key: value[key]
                            for key in ("display_name", "kind", "release_tier", "execution_classes")
                        },
                    }
                )
            wire = {
                "object": "nexus.list",
                "data": data,
                "has_more": more,
                "next_cursor": _encode_cursor(data[-1]["id"], data[-1]["revision"])
                if more
                else None,
            }
            public_response(operation, 200, {"body": wire})
        return JSONResponse(wire, headers={"Cache-Control": "no-store"})
    except Exception:
        return error("profile_result_unknown" if mutation else "profile_unavailable")


def create_profile_publications_router(
    get_provider: Callable[[], ProfileDrafts | None],
) -> APIRouter:
    router = APIRouter(tags=["profiles"])

    def handler(operation: str) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            request_id = str(request.state.request_id)
            mutation = operation in (PUBLISH, ACTIVATE)
            provider = get_provider()
            if provider is None:
                return profile_error(
                    "profile_result_unknown" if mutation else "profile_unavailable",
                    request_id,
                    operation=operation,
                )
            try:
                pairs = list(request.query_params.multi_items())
                if len(pairs) != len({k for k, _ in pairs}):
                    raise ValueError
                headers = {}
                for name in ("x-request-id", "idempotency-key"):
                    values = request.headers.getlist(name)
                    if len(values) > 1:
                        raise ValueError
                    if values:
                        headers[name] = values[0]
                envelope: dict[str, Any] = {
                    "path": request.path_params,
                    "headers": headers,
                    "query": dict(pairs),
                }
                if mutation:
                    # Reuse duplicate-key/nonfinite rejection from the bounded draft receiver.
                    from speechnexus_backend.speech.interfaces.http.profiles import (
                        _constant,
                        _object,
                    )

                    envelope["body"] = json.loads(
                        (await request.body()).decode(),
                        object_pairs_hook=_object,
                        parse_constant=_constant,
                    )
                else:
                    if request.headers.getlist("content-length") not in (
                        [],
                        ["0"],
                    ) or request.headers.getlist("transfer-encoding"):
                        raise ValueError
                    async with asyncio.timeout(1):
                        for _ in range(8):
                            message = await request.receive()
                            if message["type"] != "http.request" or message.get("body"):
                                raise ValueError
                            if not message.get("more_body", False):
                                break
                        else:
                            raise ValueError
                return await run_in_threadpool(
                    _execute, cast(ProfilePublications, provider), operation, envelope, request_id
                )
            except ValueError, TypeError, RecursionError, TimeoutError:
                return profile_error("invalid_profile_request", request_id, operation=operation)

        return handle

    for path, operation, method in (
        (PUBLIC_PATH, LIST, "GET"),
        (FIXED_PATH, PUBLIC_READ, "GET"),
        (ADMIN_REVISION + "/publications", PUBLISH, "POST"),
        (ADMIN_REVISION + "/activations", ACTIVATE, "POST"),
    ):
        router.add_api_route(path, handler(operation), methods=[method], operation_id=operation)
    return router
