"""Evidence DDL, grants and the admin wire projection stay honest and narrow."""

from __future__ import annotations

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.binding_publication import PublishedBinding
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    ADMIN_READER_TABLES,
    PUBLICATION_EVIDENCE_DDL,
    TABLE,
    binding_admin_reader_grants,
    publication_evidence_grants,
)
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
)
from speechnexus_backend.speech.interfaces.binding_drafts import (
    draft_to_wire,
    prepare_binding_draft,
    revision_to_wire,
)
from tests.support.binding_examples import binding_request

CAPACITY = "cap-asr-quality-7"


def draft() -> BindingDraft:
    command = prepare_binding_draft(binding_request(), "unit-evidence-key")
    return BindingDraft(
        command.binding_id, "1", "mbrev-unit-0001", command.definition, 1_700_000_000_000_000
    )


def test_evidence_ddl_references_publication_and_records_both_identities() -> None:
    ddl = "\n".join(PUBLICATION_EVIDENCE_DDL)

    assert f"CREATE TABLE {TABLE}" in ddl
    assert "PRIMARY KEY (kind, object_id, revision)" in ddl
    assert "REFERENCES speech_publications (kind, object_id, revision)" in ddl
    assert "capacity_manifest_id" in ddl
    assert "qualification_report_id" in ddl
    assert "evidence_source IN ('operator_cli', 'publish_http', 'backfill')" in ddl


def test_publication_writer_may_insert_but_not_rewrite_evidence() -> None:
    grants = publication_evidence_grants("speechnexus_test", "publication_writer")
    joined = " ".join(grants)

    assert 'GRANT SELECT, INSERT ON "speechnexus_test".' + TABLE in joined
    assert not any(word in joined for word in ("UPDATE", "DELETE", "TRUNCATE"))


def test_admin_reader_is_select_only_and_never_the_public_reader() -> None:
    grants = binding_admin_reader_grants("speechnexus_test", "admin_reader")
    joined = " ".join(grants)

    assert "GRANT SELECT ON" in joined
    assert TABLE in joined and "speech_publications" in joined
    assert not any(word in joined for word in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"))
    assert "speech_binding_revisions" in ADMIN_READER_TABLES

    public = " ".join(model_catalog_reader_grants("speechnexus_test", "catalogue_reader"))
    assert TABLE not in public


def test_draft_projection_has_no_publication_facts() -> None:
    wire = draft_to_wire(draft())

    assert wire["publication"] == {
        "status": "draft",
        "published_at": None,
        "retired_at": None,
        "capacity_manifest_id": None,
        "qualification_report_id": None,
        "disabled_reason": None,
    }


def test_published_projection_carries_stored_evidence() -> None:
    wire = revision_to_wire(
        draft(),
        PublishedBinding(
            published_at_us=1_700_000_000_000_000,
            capacity_manifest_id=CAPACITY,
            qualification_report_id="qrep-asr-quality-7",
        ),
    )

    publication = wire["publication"]
    assert publication["status"] == "published"
    assert publication["published_at"] == "2023-11-14T22:13:20.000000Z"
    assert publication["capacity_manifest_id"] == CAPACITY
    assert publication["qualification_report_id"] == "qrep-asr-quality-7"
    assert publication["retired_at"] is None
    assert publication["disabled_reason"] is None


def test_published_projection_rejects_an_identifier_that_breaks_the_contract() -> None:
    with pytest.raises(ContractValidationError):
        revision_to_wire(
            draft(),
            PublishedBinding(
                published_at_us=1_700_000_000_000_000,
                capacity_manifest_id="Cap Invalid",
                qualification_report_id="qrep-asr-quality-7",
            ),
        )
