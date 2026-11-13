"""Deployment-owned reviewed evidence: exact binding, stable refusal, named startup.

These tests exercise the file parser and the read-only authority port. They never
claim the reviewed facts are true: the document is a deployment's review record.
"""

import json
from pathlib import Path

import pytest

from speechnexus_backend.speech.application.profile_documents import encode_document
from speechnexus_backend.speech.application.profile_lifecycle import observe_publication_evidence
from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.domain.profile_lifecycle import (
    ProfilePublicationIneligibleError,
)
from speechnexus_backend.speech.infrastructure.profile_publication_evidence import (
    EVIDENCE_FILE_VARIABLE,
    EVIDENCE_FORMAT,
    DeploymentFilePublicationVerifier,
    load_reviewed_publications,
    parse_reviewed_publications,
)
from tests.unit.test_profile_lifecycle import prepare, request, target


def _digests() -> tuple[str, str, str, str]:
    command = prepare()
    value = request()
    return (
        value["path"]["profile_id"],
        value["path"]["revision"],
        document_digest(target(command).definition),
        document_digest(encode_document(value["body"]["validation"])),
    )


def _document(*, definition: str | None = None, validation: str | None = None) -> bytes:
    identity, revision, definition_digest, validation_digest = _digests()
    return json.dumps(
        {
            "format": EVIDENCE_FORMAT,
            "entries": [
                {
                    "profile_id": identity,
                    "revision": revision,
                    "definition_digest": definition or definition_digest,
                    "validation_digest": validation or validation_digest,
                    "reviewed_by": "release-review",
                    "reviewed_at": "2026-11-20T09:00:00Z",
                }
            ],
        }
    ).encode()


def _reviewed(raw: bytes) -> DeploymentFilePublicationVerifier:
    return DeploymentFilePublicationVerifier(parse_reviewed_publications(raw))


def test_reviewed_definition_and_validation_bytes_are_required_together() -> None:
    command = prepare()
    draft = target(command)
    observation = observe_publication_evidence(command, draft, _reviewed(_document()))
    assert (
        observation.profile_id,
        observation.revision,
        observation.definition_digest,
        observation.intent_digest,
    ) == (
        command.profile_id,
        command.revision,
        document_digest(draft.definition),
        command.request_digest,
    )
    # The authority digest names the reviewed record, not the caller's request.
    authority = observation.authority_digest
    assert authority.startswith("sha256:") and len(authority) == len("sha256:") + 64


def test_unreviewed_revision_definition_or_validation_is_a_stable_refusal() -> None:
    command = prepare()
    draft = target(command)
    unavailable = _reviewed(json.dumps({"format": EVIDENCE_FORMAT, "entries": []}).encode())
    with pytest.raises(ProfilePublicationIneligibleError):
        observe_publication_evidence(command, draft, unavailable)
    for raw in (
        _document(definition="sha256:" + "b" * 64),
        _document(validation="sha256:" + "c" * 64),
    ):
        with pytest.raises(ProfilePublicationIneligibleError):
            observe_publication_evidence(command, draft, _reviewed(raw))


def test_authority_digest_is_deterministic_for_the_same_reviewed_record() -> None:
    command = prepare()
    first = observe_publication_evidence(command, target(command), _reviewed(_document()))
    second = observe_publication_evidence(command, target(command), _reviewed(_document()))
    assert first.authority_digest == second.authority_digest


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'{"format":"speechnexus.profile-publication-evidence/1"}',
        b'{"format":"other/1","entries":[]}',
        b'{"format":"speechnexus.profile-publication-evidence/1","entries":{}}',
        b'{"format":"speechnexus.profile-publication-evidence/1","entries":[],"extra":1}',
        b'{"format":"speechnexus.profile-publication-evidence/1","entries":[],'
        b'"format":"speechnexus.profile-publication-evidence/1"}',
        b'{"format":"speechnexus.profile-publication-evidence/1","entries":[{"profile_id":'
        b'"caption-draft","revision":"1","definition_digest":"bad","validation_digest":'
        b'"sha256:' + b"a" * 64 + b'","reviewed_by":"review","reviewed_at":"2026-11-20"}]}',
        b'{"format":"speechnexus.profile-publication-evidence/1","entries":[{"profile_id":'
        b'"caption-draft","revision":"1","definition_digest":"sha256:'
        + b"a" * 64
        + b'","validation_digest":"sha256:'
        + b"a" * 64
        + b'","reviewed_by":"review","reviewed_at":"yesterday"}]}',
        b'{"format":"speechnexus.profile-publication-evidence/1","entries":[{"profile_id":'
        b'"caption-draft","revision":"1","definition_digest":"sha256:'
        + b"a" * 64
        + b'","validation_digest":"sha256:'
        + b"a" * 64
        + b'","reviewed_by":"","reviewed_at":"2026-11-20T09:00:00Z"}]}',
    ],
)
def test_invalid_documents_name_the_variable(raw: bytes) -> None:
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        parse_reviewed_publications(raw)


def test_duplicate_revision_is_rejected_instead_of_last_write_wins() -> None:
    identity, revision, definition_digest, validation_digest = _digests()
    entry = {
        "profile_id": identity,
        "revision": revision,
        "definition_digest": definition_digest,
        "validation_digest": validation_digest,
        "reviewed_by": "release-review",
        "reviewed_at": "2026-11-20T09:00:00Z",
    }
    raw = json.dumps({"format": EVIDENCE_FORMAT, "entries": [entry, entry]}).encode()
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        parse_reviewed_publications(raw)


def test_absent_document_names_the_variable_instead_of_reporting_unavailable(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "profile-publication-evidence.json"
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        load_reviewed_publications(missing)
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        # A directory is not a readable evidence document either.
        load_reviewed_publications(tmp_path)


def test_oversized_document_is_rejected_before_parsing() -> None:
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        parse_reviewed_publications(b"{" + b" " * (2 * 1024 * 1024) + b"}")
