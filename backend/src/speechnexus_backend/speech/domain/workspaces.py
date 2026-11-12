"""Shared content groups, bounded read values and accepted write intents.

A workspace is a content and budget group, not a user, tenant, ACL or privacy
boundary. Writes carry only what the frozen administrative contract declares:
a display name, an optional policy identity and, for updates, both the expected
metadata revision and at least one changed field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import counter, digest, identifier

type WorkspaceStatus = Literal["active", "disabled"]
type WorkspaceOperation = Literal["create", "update"]

MIN_IDEMPOTENCY_KEY = 8
MAX_IDEMPOTENCY_KEY = 128


class WorkspaceUnavailableError(Exception):
    """Authoritative workspace facts could not be read safely."""


class WorkspaceNotFoundError(Exception):
    """The requested group has no authoritative record."""


class WorkspacePolicyUnsupportedError(Exception):
    """This deployment has no policy registry that could resolve ``policy_id``."""


class WorkspaceConflictError(Exception):
    """A command contradicts an accepted intent, a stored revision or identity.

    Every code is a stable public error; the transport maps it without
    reinterpreting the cause:

    ``idempotency_conflict``
        The key was already accepted for a different operation, target or body.
    ``configuration_changed``
        ``expected_metadata_revision`` no longer matches the stored revision.
    ``workspace_identity_conflict``
        The proposed workspace identity is already taken by another group.
    """

    def __init__(
        self,
        code: Literal[
            "idempotency_conflict", "configuration_changed", "workspace_identity_conflict"
        ],
    ) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    display_name: str
    status: WorkspaceStatus
    metadata_revision: int
    policy_revision: int
    created_at_us: int

    def __post_init__(self) -> None:
        identifier(self.workspace_id)
        if not isinstance(self.display_name, str) or not 1 <= len(self.display_name) <= 128:
            raise ValueError("Invalid workspace display name")
        if self.status not in ("active", "disabled"):
            raise ValueError("Invalid workspace status")
        counter(self.metadata_revision, minimum=1)
        # The public policy revision is a lossless decimal string, unlike the
        # numeric metadata revision. Preserve the complete stored BIGINT.
        counter(self.policy_revision, minimum=1, maximum=2**63 - 1)
        counter(self.created_at_us)


@dataclass(frozen=True, slots=True)
class WorkspaceQuery:
    limit: int
    after_id: str | None = None

    def __post_init__(self) -> None:
        counter(self.limit, minimum=1, maximum=100)
        if self.after_id is not None:
            identifier(self.after_id)


@dataclass(frozen=True, slots=True)
class WorkspacePage:
    items: tuple[Workspace, ...]
    next_id: str | None


def check_display_name(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("Invalid workspace display name")
    return value


def check_request_id(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("Invalid request identity")
    return value


@dataclass(frozen=True, slots=True)
class WorkspaceCreateCommand:
    """A create intent; the proposed identity is allocated per attempt.

    The identity is deliberately outside the intent digest: a retried attempt
    allocates a new candidate, and an accepted original still wins.
    """

    display_name: str
    request_id: str
    key_digest: str | None
    intent_digest: str

    def __post_init__(self) -> None:
        check_display_name(self.display_name)
        check_request_id(self.request_id)
        if self.key_digest is not None:
            digest(self.key_digest)
        digest(self.intent_digest)


@dataclass(frozen=True, slots=True)
class WorkspaceUpdateCommand:
    """One accepted display/status change against an expected metadata revision."""

    workspace_id: str
    expected_metadata_revision: int
    request_id: str
    key_digest: str | None
    intent_digest: str
    display_name: str | None = None
    status: WorkspaceStatus | None = None

    def __post_init__(self) -> None:
        identifier(self.workspace_id)
        counter(self.expected_metadata_revision, minimum=1)
        check_request_id(self.request_id)
        if self.key_digest is not None:
            digest(self.key_digest)
        digest(self.intent_digest)
        if self.display_name is None and self.status is None:
            raise ValueError("A workspace update changes at least one field")
        if self.display_name is not None:
            check_display_name(self.display_name)
        if self.status is not None and self.status not in ("active", "disabled"):
            raise ValueError("Invalid workspace status")


@dataclass(frozen=True, slots=True)
class WorkspaceWriteReceipt:
    """The authoritative record an accepted command resolves to.

    A replay returns the current state of the same identity, never a second
    record: later unrelated metadata changes are visible, the identity is not.
    """

    workspace: Workspace
    replayed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace) or not isinstance(self.replayed, bool):
            raise ValueError("Invalid workspace write receipt")
