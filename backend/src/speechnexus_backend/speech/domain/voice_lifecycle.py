"""Voice retirement commands and the resulting immutable publication state.

A retirement never rewrites the published revision.  It appends one tombstone
that hides the revision from the default catalogue while the exact revision
stays readable.  The store, not the transport, decides whether the named
revision is still the selected one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

type VoiceRetirementMode = Literal["retire", "disable"]

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_REVISION = re.compile(r"[1-9][0-9]{0,18}\Z")


class VoiceRevisionNotFoundError(LookupError):
    """The voice or revision was never published."""


class VoiceRetirementConflictError(ValueError):
    """The command does not describe the currently selected revision."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VoiceRetirementUnavailableError(LookupError):
    """Retirement storage could not answer; success must not be assumed."""


@dataclass(frozen=True, slots=True)
class VoiceRetirementCommand:
    voice_id: str
    revision: str
    mode: VoiceRetirementMode
    reason: str | None
    request_id: str

    def __post_init__(self) -> None:
        for value in (self.voice_id, self.request_id):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError("Invalid voice retirement identity")
        if not isinstance(self.revision, str) or not _REVISION.fullmatch(self.revision):
            raise ValueError("Invalid voice revision")
        if self.mode not in ("retire", "disable"):
            raise ValueError("Invalid voice retirement mode")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 2000
        ):
            raise ValueError("Invalid voice retirement reason")


@dataclass(frozen=True, slots=True)
class VoiceRetirementReceipt:
    """The stored tombstone; reading the public document is a separate step."""

    voice_id: str
    revision: str
    revision_id: str
    mode: VoiceRetirementMode
    reason: str | None
    retired_at_us: int
    replayed: bool
