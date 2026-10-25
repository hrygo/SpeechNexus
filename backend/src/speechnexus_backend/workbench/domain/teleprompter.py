"""Project-scoped teleprompter position; matching, epochs and reading policy stay client-side.

The domain owns one decision only: a match produced for an older script revision may
not move the accepted position. Stream epochs, hypothesis matching and manual recovery
points are runtime concerns that never enter this persisted state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
    validate_revision_number,
)

# A visible position travels as a JSON integer; the contract leaves the upper bound to
# the producer, so it is bounded by the largest exactly representable JSON integer.
MAX_VISIBLE_POSITION = 2**53 - 1


@dataclass(frozen=True, slots=True)
class TeleprompterState:
    """Last accepted script revision, visible position and manual takeover."""

    project_id: str
    script_revision: int
    visible_position: int
    manual_hold: bool
    updated_at: datetime

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or not 1 <= len(self.project_id) <= 128:
            raise InvalidVersionChangeError("invalid teleprompter project identity")
        validate_revision_number(self.script_revision)
        if type(self.visible_position) is not int or not (
            0 <= self.visible_position <= MAX_VISIBLE_POSITION
        ):
            raise InvalidVersionChangeError("teleprompter position outside the supported range")
        if type(self.manual_hold) is not bool:
            raise InvalidVersionChangeError("teleprompter manual hold must be a boolean")
        if type(self.updated_at) is not datetime or self.updated_at.utcoffset() is None:
            raise InvalidVersionChangeError("teleprompter time must be aware")

    def advance(
        self,
        *,
        script_revision: int,
        visible_position: int,
        manual_hold: bool,
        updated_at: datetime,
    ) -> TeleprompterState:
        """Accept the same or a newer script revision; reject one already superseded.

        An equal revision moves position and takeover because the operator may seek
        backwards by hand. Only a strictly older revision is refused, so a late match
        from a previous script revision or stream epoch cannot drag the script back.
        """
        validate_revision_number(script_revision)
        if script_revision < self.script_revision:
            raise RevisionConflictError(script_revision, self.script_revision)
        return TeleprompterState(
            project_id=self.project_id,
            script_revision=script_revision,
            visible_position=visible_position,
            manual_hold=manual_hold,
            updated_at=updated_at,
        )

    @classmethod
    def initial(
        cls,
        *,
        project_id: str,
        script_revision: int,
        visible_position: int,
        manual_hold: bool,
        updated_at: datetime,
    ) -> TeleprompterState:
        """First accepted state for a project that has never stored a position."""
        return cls(
            project_id=project_id,
            script_revision=script_revision,
            visible_position=visible_position,
            manual_hold=manual_hold,
            updated_at=updated_at,
        )
