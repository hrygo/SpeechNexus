"""Read-only lookup of the identity a publication recorded for one evidence document.

The application boundary validates the identifier, asks the read-only store for
the recorded identity and turns any storage failure into one unavailable result.
It never synthesizes a digest, a recording time or a release-evidence locator.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.binding_drafts import BindingUnavailableError
from speechnexus_backend.speech.domain.binding_evidence import RecordedEvidenceIdentity


class EvidenceKind(StrEnum):
    """The two release-evidence documents a publication names."""

    CAPACITY_MANIFEST = "capacity_manifest"
    QUALIFICATION_REPORT = "qualification_report"


class BindingEvidenceReader(Protocol):
    """Read-only projection over recorded publication evidence rows."""

    def recorded_evidence(
        self, kind: EvidenceKind, identity: str
    ) -> RecordedEvidenceIdentity | None: ...


class BindingEvidenceUnavailableError(ValueError):
    """The evidence store could not be read; this is not evidence of absence."""


class BindingEvidenceService:
    """Answer one evidence-identity lookup without changing any recorded fact."""

    def __init__(self, reader: BindingEvidenceReader) -> None:
        self._reader = reader

    def capacity_manifest(self, manifest_id: str) -> RecordedEvidenceIdentity | None:
        return self._lookup(EvidenceKind.CAPACITY_MANIFEST, manifest_id)

    def qualification_report(self, report_id: str) -> RecordedEvidenceIdentity | None:
        return self._lookup(EvidenceKind.QUALIFICATION_REPORT, report_id)

    def _lookup(self, kind: EvidenceKind, identity: str) -> RecordedEvidenceIdentity | None:
        identifier(identity)
        try:
            recorded = self._reader.recorded_evidence(kind, identity)
        except BindingUnavailableError:
            raise BindingEvidenceUnavailableError from None
        if recorded is not None and recorded.identity != identity:
            raise BindingEvidenceUnavailableError
        return recorded
