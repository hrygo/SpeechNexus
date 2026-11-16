"""Version identity of this process: package version and canonical contract digest.

`GET /v1/nexus/version` exists so a consumer can tell whether it is talking to a
process generated from the same contracts (#853). Each Web bundle bakes the
digest of `packages/contracts` in at build time and compares it here on
start-up, so a mixed deployment — a rolled-back API behind an upgraded frontend,
or the reverse — becomes visible instead of silently reading new artifacts with
old types.

The digest comes from the generated `speechnexus_contracts.contract_digest`
module, which the contract generator derives from the same `contracts/` bytes
that produce the Python and TypeScript consumers. It is not a git revision, and
the response carries no endpoint, identity, credential or filesystem path. It is
a build fact, so it is never cached: a cached answer would defeat the check.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from speechnexus_contracts.contract_digest import CONTRACT_DIGEST
from speechnexus_contracts.version_http import validate_response
from starlette.responses import JSONResponse, Response

OPERATION = "getVersionIdentity"
PATH = "/v1/nexus/version"


def version_identity_body(version: str) -> dict[str, object]:
    """The single source of this response's body, shared with the unit tests."""
    return {"version": version, "contract_digest": CONTRACT_DIGEST}


def create_version_identity_router(*, version: str) -> APIRouter:
    router = APIRouter(tags=["operations"])

    @router.get(PATH, operation_id=OPERATION)
    async def get_version_identity(request: Request) -> Response:
        request_id = str(request.state.request_id)
        body = version_identity_body(version)
        # The declared contract is enforced by the generated validators, not by
        # this handler's type: a shape change on either side fails here first.
        validate_response(
            OPERATION,
            200,
            {
                "body": body,
                "headers": {"X-Request-ID": request_id, "Cache-Control": "no-store"},
            },
        )
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    return router
