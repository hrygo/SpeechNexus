"""Deployment-issued gateway credential, separate from public and controller keys."""

import hmac
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from speechnexus_gateway.domain.submission import ComponentCredentialInvalidError

_BEARER = HTTPBearer(
    auto_error=False, scheme_name="ComponentBearer", bearerFormat="opaque-component-token"
)


class ComponentCredential:
    def __init__(self, token: SecretStr) -> None:
        self._expected = token.get_secret_value().encode("utf-8")
        if not self._expected:
            raise ValueError("the gateway component credential must not be empty")

    async def __call__(
        self, credential: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]
    ) -> None:
        if credential is None or not hmac.compare_digest(
            credential.credentials.encode("utf-8"), self._expected
        ):
            raise ComponentCredentialInvalidError("the gateway component credential is invalid")
