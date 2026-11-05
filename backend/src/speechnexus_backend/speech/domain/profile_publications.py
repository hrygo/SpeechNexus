"""Publication facts and original results, separate from mutable family heads."""

from dataclasses import dataclass, field
from typing import Literal

from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.domain.profile_drafts import ProfileDraft


@dataclass(frozen=True, slots=True)
class PublishedProfile:
    draft: ProfileDraft
    validation: bytes = field(repr=False)
    published_at_us: int
    status: Literal["published", "retired", "disabled"] = "published"
    retired_at_us: int | None = None
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        document_digest(self.validation)
        if type(self.published_at_us) is not int or self.published_at_us < 0:
            raise ValueError("Invalid profile publication time")
        if self.status not in ("published", "retired", "disabled"):
            raise ValueError("Invalid profile publication state")
        if self.retired_at_us is not None and (
            type(self.retired_at_us) is not int or self.retired_at_us < self.published_at_us
        ):
            raise ValueError("Invalid profile retirement time")
        if (
            (
                self.status == "published"
                and (self.retired_at_us is not None or self.disabled_reason is not None)
            )
            or (
                self.status == "retired"
                and (self.retired_at_us is None or self.disabled_reason is not None)
            )
            or (
                self.status == "disabled"
                and (
                    not isinstance(self.disabled_reason, str)
                    or not 1 <= len(self.disabled_reason) <= 2000
                )
            )
        ):
            raise ValueError("Inconsistent profile publication state")


@dataclass(frozen=True, slots=True)
class ProfileLifecycleResult:
    outcome: Literal["completed", "replayed", "rejected", "conflict", "unknown"]
    publication: PublishedProfile | None = None
    error_code: str | None = None
