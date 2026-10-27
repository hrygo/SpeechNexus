"""Publish one stored binding revision; this operation never creates a draft.

The public read path joins ``speech_publications`` to the revision identity it
names, so publication must name an existing immutable revision. Storage has no
foreign key enforcing that link, which is why the command validates the exact
``(binding_id, revision, revision_id)`` triple in the same transaction that
appends the publication row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from speechnexus_backend.speech.domain.binding_drafts import BindingDraft
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingPublicationEvidence,
    EvidenceSource,
)
from speechnexus_backend.speech.domain.publications import Publication


class BindingRevisionNotFoundError(LookupError):
    """The exact revision identity to publish is not stored."""

    def __init__(
        self,
        binding_id: str,
        revision: int,
        revision_id: str,
        *,
        stored_revision_id: str | None = None,
    ) -> None:
        self.binding_id = binding_id
        self.revision = revision
        self.revision_id = revision_id
        self.stored_revision_id = stored_revision_id
        if stored_revision_id is None:
            detail = (
                f"speech_binding_revisions has no row for binding_id={binding_id} "
                f"revision={revision}"
            )
        else:
            detail = (
                f"binding_id={binding_id} revision={revision} stores "
                f"revision_id={stored_revision_id}, not {revision_id}"
            )
        super().__init__(detail)


class BindingPublicationConflictError(ValueError):
    """The same revision number already names a different published document."""

    def __init__(
        self,
        binding_id: str,
        revision: int,
        stored_revision_id: str,
        requested_revision_id: str,
    ) -> None:
        self.binding_id = binding_id
        self.revision = revision
        self.stored_revision_id = stored_revision_id
        self.requested_revision_id = requested_revision_id
        super().__init__(
            f"binding_id={binding_id} revision={revision} is already published as "
            f"revision_id={stored_revision_id}, not {requested_revision_id}"
        )


class BindingPublicationStore(Protocol):
    def publish_binding_revision(
        self,
        publication: Publication,
        evidence: BindingPublicationEvidence,
    ) -> tuple[Publication, BindingPublicationEvidence, bool]:
        """Validate the exact stored revision, then append row and evidence together."""
        ...


@dataclass(frozen=True, slots=True)
class PublishedBinding:
    """The facts an admin GET may report for a proven published revision."""

    published_at_us: int
    capacity_manifest_id: str
    qualification_report_id: str


class BindingPublicationFacts(Protocol):
    def project(self, draft: BindingDraft) -> PublishedBinding | None:
        """Return published facts, ``None`` for a draft, or fail closed."""
        ...


@dataclass(frozen=True, slots=True)
class BindingPublicationResult:
    """One publication fact, its evidence and whether this invocation inserted it."""

    publication: Publication
    evidence: BindingPublicationEvidence
    created: bool


class BindingPublisher:
    """Promote one existing revision; a replay reports the stored fact unchanged."""

    def __init__(
        self,
        store: BindingPublicationStore,
        *,
        now_us: Callable[[], int],
    ) -> None:
        self._store = store
        self._now_us = now_us

    def publish(
        self,
        binding_id: str,
        revision: int,
        revision_id: str,
        *,
        capacity_manifest_id: str,
        qualification_report_id: str,
        capacity_manifest_digest: str | None = None,
        qualification_report_digest: str | None = None,
        evidence_source: EvidenceSource = "operator_cli",
    ) -> BindingPublicationResult:
        now_us = self._now_us()
        publication = Publication(
            kind="binding",
            object_id=binding_id,
            revision=revision,
            revision_id=revision_id,
            published_at_us=now_us,
        )
        evidence = BindingPublicationEvidence(
            object_id=binding_id,
            revision=revision,
            revision_id=revision_id,
            capacity_manifest_id=capacity_manifest_id,
            qualification_report_id=qualification_report_id,
            evidence_source=evidence_source,
            recorded_at_us=now_us,
            capacity_manifest_digest=capacity_manifest_digest,
            qualification_report_digest=qualification_report_digest,
        )
        stored, stored_evidence, created = self._store.publish_binding_revision(
            publication, evidence
        )
        return BindingPublicationResult(stored, stored_evidence, created)
