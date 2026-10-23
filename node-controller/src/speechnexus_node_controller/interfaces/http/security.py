"""Per-node component credential of the internal runtime-control protocol.

The credential is issued by the deployment and is separate from scheduler,
gateway, public, and admin credentials. It is a component credential, not a
user identity: the controller still decides what an authenticated caller may do
from the intent identity it submits.
"""

from __future__ import annotations

import hmac

from fastapi import Request
from pydantic import SecretStr

from speechnexus_node_controller.domain.intents import ComponentCredentialInvalidError


class ComponentCredential:
    """Callable FastAPI dependency that validates the bearer component token."""

    def __init__(self, token: SecretStr) -> None:
        value = token.get_secret_value()
        if not value:
            raise ValueError("the runtime-control component credential must not be empty")
        self._expected = value.encode("utf-8")

    async def __call__(self, request: Request) -> None:
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            credential.encode("utf-8"), self._expected
        ):
            raise ComponentCredentialInvalidError(
                "the component credential is missing or does not match this node controller"
            )
