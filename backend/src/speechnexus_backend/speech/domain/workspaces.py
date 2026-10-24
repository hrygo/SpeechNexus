"""Shared content groups and bounded read values, independent of wire DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import counter, identifier


class WorkspaceUnavailableError(Exception):
    """Authoritative workspace facts could not be read safely."""


class WorkspaceNotFoundError(Exception):
    """The requested group has no authoritative record."""


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    display_name: str
    status: Literal["active", "disabled"]
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
