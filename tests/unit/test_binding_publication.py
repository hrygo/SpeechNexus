"""Publication command identity and operator-visible failures stay explicit."""

from __future__ import annotations

import pytest

from speechnexus_backend.publish_binding import main
from speechnexus_backend.speech.application.binding_publication import (
    BindingPublicationConflictError,
    BindingPublicationResult,
    BindingPublisher,
    BindingRevisionNotFoundError,
)
from speechnexus_backend.speech.domain.publications import Publication


class Store:
    def __init__(self, stored: Publication | None = None) -> None:
        self.stored = stored
        self.requested: list[Publication] = []

    def publish_binding_revision(self, publication: Publication) -> tuple[Publication, bool]:
        self.requested.append(publication)
        if self.stored is not None:
            return self.stored, False
        return publication, True


def publisher(store: Store, clock: int = 1_700_000_000_000_000) -> BindingPublisher:
    return BindingPublisher(store, now_us=lambda: clock)


def test_publish_constructs_one_binding_publication_identity() -> None:
    store = Store()

    result = publisher(store).publish("asr-quality", 3, "mbrev-abc123")

    assert result == BindingPublicationResult(
        Publication("binding", "asr-quality", 3, "mbrev-abc123", 1_700_000_000_000_000),
        True,
    )
    assert store.requested == [result.publication]


def test_replay_reports_the_stored_publication_unchanged() -> None:
    stored = Publication("binding", "asr-quality", 3, "mbrev-abc123", 1_700_000_000_000_001)

    result = publisher(Store(stored)).publish("asr-quality", 3, "mbrev-abc123")

    assert result.publication == stored
    assert result.created is False


def test_missing_revision_names_the_identity_that_was_not_stored() -> None:
    error = BindingRevisionNotFoundError("asr-quality", 3, "mbrev-abc123")

    assert error.binding_id == "asr-quality"
    assert error.revision == 3
    assert "no row" in str(error)
    assert "binding_id=asr-quality" in str(error)
    assert "revision=3" in str(error)


def test_different_stored_revision_id_names_both_identities() -> None:
    error = BindingRevisionNotFoundError(
        "asr-quality", 3, "mbrev-abc123", stored_revision_id="mbrev-stored456"
    )

    assert "mbrev-stored456" in str(error)
    assert "mbrev-abc123" in str(error)


def test_conflict_states_both_published_and_requested_revision_ids() -> None:
    error = BindingPublicationConflictError("asr-quality", 3, "mbrev-stored456", "mbrev-abc123")

    assert "already published as revision_id=mbrev-stored456" in str(error)
    assert "not mbrev-abc123" in str(error)


def test_command_fails_when_the_writer_dsn_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as stopped:
        main(["--binding-id", "asr-quality", "--revision", "3", "--revision-id", "mbrev-abc123"])

    assert stopped.value.code == 2


@pytest.mark.parametrize("revision", ["0", "-1", "01", "abc"])
def test_command_rejects_a_revision_outside_the_stored_decimal_form(
    monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", "postgresql://example/db")

    with pytest.raises(SystemExit) as stopped:
        main(
            ["--binding-id", "asr-quality", "--revision", revision, "--revision-id", "mbrev-abc123"]
        )

    assert stopped.value.code == 2
