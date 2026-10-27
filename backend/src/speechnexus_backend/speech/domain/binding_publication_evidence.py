"""Publication-time evidence for one immutable binding revision.

Evidence is not part of the revision definition: the definition is fixed when the
draft is created, while the capacity manifest and qualification report become
known only when an operator publishes it. Keeping evidence in its own table also
keeps those identifiers out of the public catalogue reader's authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.configuration import MAX_REVISION

EvidenceSource = Literal["operator_cli", "publish_http", "backfill"]
EVIDENCE_SOURCES: tuple[EvidenceSource, ...] = ("operator_cli", "publish_http", "backfill")
MAX_RECORDED_AT_US = 253_402_300_799_999_999
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class BindingEvidenceConflictError(ValueError):
    """A publication already carries different evidence facts."""


class BindingCapacityManifestMismatchError(ValueError):
    """The declared capacity manifest does not match the revision's execution recipe."""

    def __init__(self, binding_id: str, revision: int, expected: str, declared: str) -> None:
        self.binding_id = binding_id
        self.revision = revision
        self.expected = expected
        self.declared = declared
        super().__init__(
            f"binding_id={binding_id} revision={revision} execution_recipe declares "
            f"capacity_manifest_id={expected}, not {declared}"
        )


@dataclass(frozen=True, slots=True)
class BindingPublicationEvidence:
    """The two evidence identities recorded when a revision becomes public."""

    object_id: str
    revision: int
    revision_id: str
    capacity_manifest_id: str
    qualification_report_id: str
    evidence_source: EvidenceSource
    recorded_at_us: int
    capacity_manifest_digest: str | None = None
    qualification_report_digest: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.object_id,
            self.revision_id,
            self.capacity_manifest_id,
            self.qualification_report_id,
        ):
            identifier(value)
        if type(self.revision) is not int or not 1 <= self.revision <= MAX_REVISION:
            raise ValueError("Invalid evidence revision")
        if self.evidence_source not in EVIDENCE_SOURCES:
            raise ValueError("Unknown evidence source")
        if (
            type(self.recorded_at_us) is not int
            or not 0 <= self.recorded_at_us <= MAX_RECORDED_AT_US
        ):
            raise ValueError("Invalid evidence timestamp")
        for digest in (self.capacity_manifest_digest, self.qualification_report_digest):
            if digest is not None and (
                not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
            ):
                raise ValueError("Invalid evidence digest")

    @property
    def kind(self) -> Literal["binding"]:
        return "binding"

    def same_facts(self, other: BindingPublicationEvidence) -> bool:
        """Compare recorded facts, not the recording time or evidence source."""
        return (
            self.object_id,
            self.revision,
            self.revision_id,
            self.capacity_manifest_id,
            self.qualification_report_id,
            self.capacity_manifest_digest,
            self.qualification_report_digest,
        ) == (
            other.object_id,
            other.revision,
            other.revision_id,
            other.capacity_manifest_id,
            other.qualification_report_id,
            other.capacity_manifest_digest,
            other.qualification_report_digest,
        )
