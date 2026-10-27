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
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingPublicationEvidence,
)
from speechnexus_backend.speech.domain.publications import Publication

CAPACITY = "cap-asr-quality-7"
REPORT = "qrep-asr-quality-7"


class Store:
    def __init__(
        self,
        stored: Publication | None = None,
        evidence: BindingPublicationEvidence | None = None,
    ) -> None:
        self.stored = stored
        self.stored_evidence = evidence
        self.requested: list[tuple[Publication, BindingPublicationEvidence]] = []

    def publish_binding_revision(
        self, publication: Publication, evidence: BindingPublicationEvidence
    ) -> tuple[Publication, BindingPublicationEvidence, bool]:
        self.requested.append((publication, evidence))
        if self.stored is not None:
            return self.stored, self.stored_evidence or evidence, False
        return publication, evidence, True


def publisher(store: Store, clock: int = 1_700_000_000_000_000) -> BindingPublisher:
    return BindingPublisher(store, now_us=lambda: clock)


def publish(store: Store, *, clock: int = 1_700_000_000_000_000) -> BindingPublicationResult:
    return publisher(store, clock).publish(
        "asr-quality",
        3,
        "mbrev-abc123",
        capacity_manifest_id=CAPACITY,
        qualification_report_id=REPORT,
    )


def test_publish_constructs_one_publication_and_matching_evidence() -> None:
    store = Store()

    result = publish(store)

    assert result.publication == Publication(
        "binding", "asr-quality", 3, "mbrev-abc123", 1_700_000_000_000_000
    )
    assert result.evidence == BindingPublicationEvidence(
        object_id="asr-quality",
        revision=3,
        revision_id="mbrev-abc123",
        capacity_manifest_id=CAPACITY,
        qualification_report_id=REPORT,
        evidence_source="operator_cli",
        recorded_at_us=1_700_000_000_000_000,
    )
    assert result.created is True
    assert store.requested == [(result.publication, result.evidence)]


def test_replay_reports_the_stored_publication_and_evidence_unchanged() -> None:
    stored = Publication("binding", "asr-quality", 3, "mbrev-abc123", 1_700_000_000_000_001)
    evidence = BindingPublicationEvidence(
        object_id="asr-quality",
        revision=3,
        revision_id="mbrev-abc123",
        capacity_manifest_id=CAPACITY,
        qualification_report_id=REPORT,
        evidence_source="operator_cli",
        recorded_at_us=1_700_000_000_000_001,
    )

    result = publish(Store(stored, evidence))

    assert result.publication == stored
    assert result.evidence == evidence
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


def test_evidence_requires_both_identifiers_and_a_known_source() -> None:
    with pytest.raises(ValueError):
        BindingPublicationEvidence(
            object_id="asr-quality",
            revision=3,
            revision_id="mbrev-abc123",
            capacity_manifest_id="",
            qualification_report_id=REPORT,
            evidence_source="operator_cli",
            recorded_at_us=1,
        )
    with pytest.raises(ValueError):
        BindingPublicationEvidence(
            object_id="asr-quality",
            revision=3,
            revision_id="mbrev-abc123",
            capacity_manifest_id=CAPACITY,
            qualification_report_id=REPORT,
            evidence_source="invented",
            recorded_at_us=1,
        )
    with pytest.raises(ValueError):
        BindingPublicationEvidence(
            object_id="asr-quality",
            revision=3,
            revision_id="mbrev-abc123",
            capacity_manifest_id=CAPACITY,
            qualification_report_id=REPORT,
            evidence_source="operator_cli",
            recorded_at_us=1,
            capacity_manifest_digest="sha256:not-hex",
        )


def test_command_fails_when_the_writer_dsn_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "--binding-id",
                "asr-quality",
                "--revision",
                "3",
                "--revision-id",
                "mbrev-abc123",
                "--capacity-manifest-id",
                CAPACITY,
                "--qualification-report-id",
                REPORT,
            ]
        )

    assert stopped.value.code == 2


def test_command_requires_both_evidence_identifiers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", "postgresql://example/db")

    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "--binding-id",
                "asr-quality",
                "--revision",
                "3",
                "--revision-id",
                "mbrev-abc123",
                "--capacity-manifest-id",
                CAPACITY,
            ]
        )

    assert stopped.value.code == 2
    assert "--qualification-report-id" in capsys.readouterr().err


@pytest.mark.parametrize("revision", ["0", "-1", "01", "abc"])
def test_command_rejects_a_revision_outside_the_stored_decimal_form(
    monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", "postgresql://example/db")

    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "--binding-id",
                "asr-quality",
                "--revision",
                revision,
                "--revision-id",
                "mbrev-abc123",
                "--capacity-manifest-id",
                CAPACITY,
                "--qualification-report-id",
                REPORT,
            ]
        )

    assert stopped.value.code == 2
