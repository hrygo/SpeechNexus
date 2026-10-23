"""Explicit synthetic authorization only; never exported by a product composition root."""

from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    RedactExecutionCommand,
)
from speechnexus_gateway.domain.submission import ComponentCredentialInvalidError


class SyntheticCommandDeletionAuthorizer:
    def __init__(self, permit: AuthorizedCommandRedaction) -> None:
        self.permit = permit
        self.calls = 0

    def authorize(self, request: RedactExecutionCommand) -> AuthorizedCommandRedaction:
        self.calls += 1
        if request != self.permit.request:
            raise ComponentCredentialInvalidError("no synthetic deletion permit for this request")
        return self.permit
