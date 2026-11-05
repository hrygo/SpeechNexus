"""Publication facts and original results, separate from mutable family heads."""

from dataclasses import dataclass, field
from typing import Literal

from speechnexus_backend.speech.domain.profile_drafts import ProfileDraft


@dataclass(frozen=True, slots=True)
class PublishedProfile:
    draft: ProfileDraft
    validation: bytes = field(repr=False)
    published_at_us: int
    status: Literal["published", "retired", "disabled"] = "published"
    retired_at_us: int | None = None
    disabled_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileLifecycleResult:
    outcome: Literal["completed", "replayed", "rejected", "conflict", "unknown"]
    publication: PublishedProfile | None = None
    error_code: str | None = None
