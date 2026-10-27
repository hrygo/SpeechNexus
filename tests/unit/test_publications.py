"""Publication commands stay append-only and define a stable catalog revision."""

from __future__ import annotations

import pytest

from speechnexus_backend.speech.application.publications import (
    PublicationAbsentError,
    PublicationConflictError,
    Publications,
)
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationKind,
    catalog_digest,
)


class Store:
    def __init__(self) -> None:
        self.rows: list[Publication] = []
        self.clock = 1_700_000_000_000_000

    def append(self, publication: Publication) -> None:
        if self.get(publication.kind, publication.object_id, publication.revision) is not None:
            raise ValueError("duplicate publication")
        self.rows.append(publication)

    def get(self, kind: PublicationKind, object_id: str, revision: int) -> Publication | None:
        for row in self.rows:
            if (row.kind, row.object_id, row.revision) == (kind, object_id, revision):
                return row
        return None

    def list_kind(self, kind: PublicationKind) -> list[Publication]:
        return [row for row in self.rows if row.kind == kind]


def build() -> tuple[Publications, Store]:
    store = Store()
    return Publications(store, now_us=lambda: store.clock), store


def test_publish_records_one_immutable_identity() -> None:
    publications, store = build()
    publication, created = publications.publish("binding", "binding-tts", 1, "binding-rev-1")

    assert created is True
    assert publication.published_at_us == store.clock
    assert publication.reference == {"id": "binding-tts", "revision": "1"}
    assert len(store.rows) == 1


def test_identical_republication_reports_the_original() -> None:
    publications, store = build()
    first, _ = publications.publish("binding", "binding-tts", 1, "binding-rev-1")
    store.clock += 5
    again, created = publications.publish("binding", "binding-tts", 1, "binding-rev-1")

    assert created is False
    assert again == first
    assert len(store.rows) == 1


def test_revision_number_cannot_be_rewritten_to_another_document() -> None:
    publications, _ = build()
    publications.publish("binding", "binding-tts", 1, "binding-rev-1")
    with pytest.raises(PublicationConflictError):
        publications.publish("binding", "binding-tts", 1, "binding-rev-2")


def test_unpublished_revision_is_absent_not_empty() -> None:
    publications, _ = build()
    with pytest.raises(PublicationAbsentError):
        publications.require("binding", "binding-tts", 1)


def test_catalog_digest_ignores_order_and_duplicates() -> None:
    first = Publication("binding", "binding-tts", 1, "binding-rev-1", 1)
    second = Publication("profile", "profile-main", 3, "profile-rev-3", 2)

    assert catalog_digest([first, second]) == catalog_digest([second, first])
    assert catalog_digest([first, second]) == catalog_digest([first, second, first])


def test_catalog_digest_changes_when_a_revision_is_published() -> None:
    publications, _ = build()
    before = publications.current_catalog_digest()
    publications.publish("binding", "binding-tts", 1, "binding-rev-1")
    after = publications.current_catalog_digest()

    assert before != after
    assert after.startswith("sha256:")


def test_invalid_publication_identity_is_rejected() -> None:
    with pytest.raises(ValueError):
        Publication("binding", "Binding-TTS", 1, "binding-rev-1", 1)
    with pytest.raises(ValueError):
        Publication("binding", "binding-tts", 0, "binding-rev-1", 1)
    with pytest.raises(ValueError):
        Publication("binding", "binding-tts", 1, "binding-rev-1", -1)
