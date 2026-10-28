"""Publish one stored binding revision; this operation never creates a draft.

The public read path joins ``speech_publications`` to the revision identity it
names, so publication must name an existing immutable revision. Storage has no
foreign key enforcing that link, which is why the command validates the exact
``(binding_id, revision, revision_id)`` triple in the same transaction that
appends the publication row.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from speechnexus_backend.speech.domain.binding_drafts import BindingDraft
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingPublicationEvidence,
    EvidenceSource,
)
from speechnexus_backend.speech.domain.configuration import revision_number
from speechnexus_backend.speech.domain.publications import Publication

MISSING_HEAD = object()


class BindingHeadPreconditionError(ValueError):
    """A new publication needs the operator's observed current revision."""


class BindingIntentConflictError(ValueError):
    """The same idempotency identity already names a different intent."""


@dataclass(frozen=True, slots=True)
class BindingPublishIntent:
    installation_id: str
    database: str
    schema: str
    expected_current_revision: str | None
    expected_catalog_revision: int
    key_digest: str

    @classmethod
    def from_request(
        cls,
        *,
        installation_id: str,
        database: str,
        schema: str,
        expected_current_revision: str | None,
        expected_catalog_revision: int,
        idempotency_key: str,
    ) -> BindingPublishIntent:
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", installation_id
        ):
            raise ValueError("Invalid installation identity")
        if not database or not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
            raise ValueError("Invalid database target")
        if type(expected_catalog_revision) is not int or expected_catalog_revision < 1:
            raise ValueError("Invalid observed catalogue revision")
        if expected_current_revision is not None:
            revision_number(expected_current_revision)
        if (
            not 1 <= len(idempotency_key) <= 256
            or not idempotency_key.isascii()
            or not idempotency_key.isprintable()
        ):
            raise ValueError("Invalid idempotency key")
        key_digest = "sha256:" + hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        return cls(
            installation_id,
            database,
            schema,
            expected_current_revision,
            expected_catalog_revision,
            key_digest,
        )


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
        *,
        expected_current_revision: object = MISSING_HEAD,
        intent: BindingPublishIntent | None = None,
    ) -> tuple[Publication, BindingPublicationEvidence, bool, str, bool]:
        """Validate the exact stored revision, then append row and evidence together."""
        ...


@dataclass(frozen=True, slots=True)
class PublishedBinding:
    """The facts an admin GET may report for a proven published revision."""

    published_at_us: int
    capacity_manifest_id: str
    qualification_report_id: str
    status: Literal["published", "retired", "disabled"] = "published"
    retired_at_us: int | None = None
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("published", "retired", "disabled"):
            raise ValueError("Invalid published binding status")
        if self.status == "published" and (
            self.retired_at_us is not None or self.disabled_reason is not None
        ):
            raise ValueError("Published binding cannot carry retirement facts")
        if self.status == "retired" and (
            self.retired_at_us is None or self.disabled_reason is not None
        ):
            raise ValueError("Retired binding requires its retirement time")
        if self.status == "disabled" and (
            self.retired_at_us is None or self.disabled_reason is None
        ):
            raise ValueError("Disabled binding requires its reason")


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
    outcome: str
    original_created: bool


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
        expected_current_revision: object = MISSING_HEAD,
        intent: BindingPublishIntent | None = None,
    ) -> BindingPublicationResult:
        if expected_current_revision is not MISSING_HEAD and expected_current_revision is not None:
            if not isinstance(expected_current_revision, str):
                raise ValueError("Invalid configuration revision")
            revision_number(expected_current_revision)
        if intent is None:
            raise BindingHeadPreconditionError("complete_publication_intent_required")
        if expected_current_revision != intent.expected_current_revision:
            raise ValueError("Observation does not match publication intent")
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
        stored, stored_evidence, created, outcome, original_created = (
            self._store.publish_binding_revision(
                publication,
                evidence,
                expected_current_revision=expected_current_revision,
                intent=intent,
            )
        )
        return BindingPublicationResult(stored, stored_evidence, created, outcome, original_created)
