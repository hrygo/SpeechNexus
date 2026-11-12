"""Binding family summaries and bounded keyset queries; no storage imports.

A family is identified by its immutable ``binding_id``, which is also the keyset
order. ``latest_revision`` is the family allocation head, ``current_revision`` is
the mutable publication pointer and is absent when the family was never published
or its head revision was retired or disabled. ``publication_status`` describes
``latest_revision`` only, so a newer draft can coexist with a different current
revision.

Every emitted value has an authoritative stored source: the family row, the
immutable revision document selected by ``latest_revision``, the publication row
and retirement tombstone for that revision, and the publication head. A missing
or inconsistent source raises instead of projecting a guessed row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import counter, identifier
from speechnexus_backend.speech.domain.configuration import revision_number

type BindingPublicationStatus = Literal["draft", "published", "retired", "disabled"]

BINDING_PUBLICATION_STATUSES: tuple[BindingPublicationStatus, ...] = (
    "draft",
    "published",
    "retired",
    "disabled",
)
# Mirrors shared/capability-domains.schema.json; the stored revision document is
# create-validated against the same closed enums.
TASK_KINDS = frozenset(
    {
        "asr_realtime",
        "asr_full_segment_review",
        "asr_file",
        "tts_realtime",
        "tts_file",
        "voice_design",
        "voice_clone",
        "voice_validation",
        "forced_alignment",
        "speaker_diarization",
        "vad",
    }
)
RELEASE_TIERS = frozenset({"experimental", "best_effort", "production"})
MAX_SUMMARY_LIMIT = 100
_LOGICAL_MODEL = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")


class BindingSummaryUnavailableError(Exception):
    """Authoritative binding family summaries could not be read or trusted."""


@dataclass(frozen=True, slots=True)
class ModelBindingSummary:
    """One family row; every field came from a named authoritative source."""

    binding_id: str
    latest_revision: str
    current_revision: str | None
    logical_model: str
    tasks: tuple[str, ...]
    release_tier: str
    publication_status: BindingPublicationStatus

    def __post_init__(self) -> None:
        identifier(self.binding_id)
        latest = revision_number(self.latest_revision)
        if not isinstance(self.logical_model, str) or not _LOGICAL_MODEL.fullmatch(
            self.logical_model
        ):
            raise ValueError("Invalid binding logical model")
        if not isinstance(self.tasks, tuple) or not self.tasks:
            raise ValueError("A binding summary requires at least one task")
        if len(set(self.tasks)) != len(self.tasks) or any(
            not isinstance(task, str) or task not in TASK_KINDS for task in self.tasks
        ):
            raise ValueError("Invalid binding task kinds")
        if not isinstance(self.release_tier, str) or self.release_tier not in RELEASE_TIERS:
            raise ValueError("Invalid binding release tier")
        if (
            not isinstance(self.publication_status, str)
            or self.publication_status not in BINDING_PUBLICATION_STATUSES
        ):
            raise ValueError("Invalid binding publication status")
        if self.current_revision is not None:
            current = revision_number(self.current_revision)
            if current > latest:
                raise ValueError("A current revision cannot exceed the latest revision")
            # The publication head is only written for a published revision and
            # deleted by retirement or disable, so these combinations mean the
            # projection mixed facts from different rows.
            if current == latest and self.publication_status != "published":
                raise ValueError("The latest revision is current without being published")


def publication_status(*, published: bool, retirement_mode: str | None) -> BindingPublicationStatus:
    """Map the exact revision's publication and tombstone facts onto the enum."""
    if type(published) is not bool:
        raise ValueError("Invalid binding publication fact")
    if retirement_mode is None:
        return "published" if published else "draft"
    if not published:
        raise ValueError("A retired binding revision has no publication row")
    if retirement_mode == "retire":
        return "retired"
    if retirement_mode == "disable":
        return "disabled"
    raise ValueError("Invalid binding retirement mode")


@dataclass(frozen=True, slots=True)
class BindingSummaryQuery:
    """One bounded keyset page request; filters are part of the cursor scope."""

    limit: int
    after_id: str | None = None
    status: BindingPublicationStatus | None = None

    def __post_init__(self) -> None:
        counter(self.limit, minimum=1, maximum=MAX_SUMMARY_LIMIT)
        if self.after_id is not None:
            identifier(self.after_id)
        if self.status is not None and (
            not isinstance(self.status, str) or self.status not in BINDING_PUBLICATION_STATUSES
        ):
            raise ValueError("Invalid binding publication status")


@dataclass(frozen=True, slots=True)
class BindingSummaryPage:
    items: tuple[ModelBindingSummary, ...]
    next_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, ModelBindingSummary) for item in self.items
        ):
            raise ValueError("Invalid binding summary page")
        if self.next_id is not None:
            identifier(self.next_id)
            if not self.items or self.next_id != self.items[-1].binding_id:
                raise ValueError("A binding page cursor must name the last emitted row")
