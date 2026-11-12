"""Recorded identity of one release-evidence document named by publication rows.

The publication evidence table is keyed by the immutable binding revision it
qualifies. A capacity manifest or qualification report identifier is therefore
not a primary key there: the same document may be named by several publication
rows. This value type carries only what those rows record — the identifier, the
document digest when a row recorded one, and the earliest recording time. It
never carries a release-evidence locator, because no column stores one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from speechnexus_backend.speech.domain.assets import identifier

MAX_RECORDED_AT_US = 253_402_300_799_999_999
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RecordedEvidenceIdentity:
    """One recorded evidence identity, independent of the publication naming it."""

    identity: str
    recorded_at_us: int
    digest: str | None = None

    def __post_init__(self) -> None:
        identifier(self.identity)
        if (
            type(self.recorded_at_us) is not int
            or not 0 <= self.recorded_at_us <= MAX_RECORDED_AT_US
        ):
            raise ValueError("Invalid evidence recording time")
        if self.digest is not None and (
            not isinstance(self.digest, str) or _DIGEST.fullmatch(self.digest) is None
        ):
            raise ValueError("Invalid evidence digest")
