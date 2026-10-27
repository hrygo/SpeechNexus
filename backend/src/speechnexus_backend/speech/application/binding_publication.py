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
    def publish_binding_revision(self, publication: Publication) -> tuple[Publication, bool]:
        """Validate the exact stored revision, then append; return row and created flag."""
        ...


@dataclass(frozen=True, slots=True)
class BindingPublicationResult:
    """One publication fact plus whether this invocation inserted it."""

    publication: Publication
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

    def publish(self, binding_id: str, revision: int, revision_id: str) -> BindingPublicationResult:
        publication = Publication(
            kind="binding",
            object_id=binding_id,
            revision=revision,
            revision_id=revision_id,
            published_at_us=self._now_us(),
        )
        stored, created = self._store.publish_binding_revision(publication)
        return BindingPublicationResult(stored, created)
