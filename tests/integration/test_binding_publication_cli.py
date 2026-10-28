"""Real PostgreSQL proof for the supported binding publication command.

The revision is produced by the product draft transaction; the command then
promotes that stored revision and the public read path resolves it. Synthetic
identities prove the chain only; they do not qualify production admission.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from alembic import command as migration
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.binding_examples import binding_request

from speechnexus_backend.publish_binding import (
    EXIT_CONFLICT,
    EXIT_NOT_STORED,
    main,
    publish_binding,
)
from speechnexus_backend.speech.application.binding_documents import decode_document
from speechnexus_backend.speech.application.binding_publication import (
    BindingPublicationConflictError,
    BindingPublicationResult,
    BindingRevisionNotFoundError,
)
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingCapacityManifestMismatchError,
    BindingEvidenceConflictError,
)
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import PostgresBindingDrafts
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    publication_evidence_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.publication_schema import publication_grants
from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_binding_publication"
REPLACEMENT_REVISION_ID = "mbrev-conflict-0001"
CAPACITY = "cap-asr-quality-7"
REPORT = "qrep-asr-quality-7"


@dataclass
class Sandbox:
    pool: SpeechPool
    admin: psycopg.Connection[tuple[object, ...]]
    schema: str
    role: str
    writer_url: str


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    base = os.environ.get(VARIABLE)
    if not base:
        pytest.skip("Configure an isolated PostgreSQL DSN; skip is not evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_pubcli_" + suffix, "sn_pubcli_role_" + suffix
    engine = create_engine(
        base.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(base, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    for grant in (
        *publication_grants(schema, role),
        *binding_draft_grants(schema, role),
        *publication_evidence_grants(schema, role),
    ):
        admin.execute(grant)
    writer_url = make_conninfo(base, user=role, password=PASSWORD)
    pool = speech_pool(writer_url, schema=schema, max_size=2, name="pub-cli-test")
    pool.open()
    try:
        yield Sandbox(pool, admin, schema, role, writer_url)
    finally:
        pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def create_draft(sandbox: Sandbox, key: str = "publish-cli-original-key") -> BindingDraft:
    result = PostgresBindingDrafts(sandbox.pool).create(
        prepare_binding_draft(binding_request(), key)
    )
    assert result.outcome == "created", result
    assert result.draft is not None
    return result.draft


def counts(sandbox: Sandbox) -> tuple[int, int, int]:
    row = sandbox.admin.execute(
        f'SELECT (SELECT count(*) FROM "{sandbox.schema}".speech_publications), '
        f'(SELECT count(*) FROM "{sandbox.schema}".speech_binding_revisions), '
        f'(SELECT count(*) FROM "{sandbox.schema}".speech_binding_publication_evidence)'
    ).fetchone()
    assert row is not None
    publications, revisions, evidence = row
    assert isinstance(publications, int) and isinstance(revisions, int)
    assert isinstance(evidence, int)
    return publications, revisions, evidence


def publish(
    sandbox: Sandbox,
    draft: BindingDraft,
    *,
    revision_id: str | None = None,
    revision: int | None = None,
    capacity_manifest_id: str = CAPACITY,
    qualification_report_id: str = REPORT,
) -> BindingPublicationResult:
    return publish_binding(
        database_url=sandbox.writer_url,
        schema=sandbox.schema,
        binding_id=draft.binding_id,
        revision=int(draft.revision) if revision is None else revision,
        revision_id=draft.revision_id if revision_id is None else revision_id,
        capacity_manifest_id=capacity_manifest_id,
        qualification_report_id=qualification_report_id,
    )


def test_publish_before_resolution_is_absent_and_after_resolves(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    jobs = PostgresPublicJobs(sandbox.pool)
    assert jobs.latest_binding(draft.binding_id) is None, "an unpublished draft must not resolve"
    assert jobs.catalog_revision() == 1
    assert counts(sandbox) == (0, 1, 0)

    result = publish(sandbox, draft)

    assert result.created is True
    assert counts(sandbox) == (1, 1, 1)
    assert jobs.catalog_revision() == 2
    fact = jobs.latest_binding(draft.binding_id)
    assert fact is not None
    assert fact.publication.kind == "binding"
    assert fact.publication.object_id == draft.binding_id
    assert str(fact.publication.revision) == draft.revision
    assert fact.publication.revision_id == draft.revision_id
    assert fact.definition == decode_document(draft.definition)


def test_repeated_publish_of_the_same_triple_keeps_the_row_count(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    first = publish(sandbox, draft)
    before = counts(sandbox)

    second = publish(sandbox, draft)
    after = counts(sandbox)

    assert first.created is True
    assert second.created is False
    assert second.publication == first.publication
    assert before == (1, 1, 1)
    assert after == (1, 1, 1)


def test_same_revision_with_another_revision_id_is_an_explicit_conflict(
    sandbox: Sandbox,
) -> None:
    draft = create_draft(sandbox)
    publish(sandbox, draft)

    with pytest.raises(BindingPublicationConflictError) as failure:
        publish(sandbox, draft, revision_id=REPLACEMENT_REVISION_ID)

    message = str(failure.value)
    assert f"already published as revision_id={draft.revision_id}" in message
    assert REPLACEMENT_REVISION_ID in message
    assert counts(sandbox) == (1, 1, 1), "a conflict must not write another row"


def test_unstored_or_mismatched_revision_is_rejected_before_insert(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)

    with pytest.raises(BindingRevisionNotFoundError) as missing:
        publish(sandbox, draft, revision=int(draft.revision) + 1)
    assert "no row" in str(missing.value)

    with pytest.raises(BindingRevisionNotFoundError) as mismatched:
        publish(sandbox, draft, revision_id=REPLACEMENT_REVISION_ID)
    assert f"stores revision_id={draft.revision_id}" in str(mismatched.value)
    assert REPLACEMENT_REVISION_ID in str(mismatched.value)
    assert counts(sandbox) == (0, 1, 0), "rejected validation must leave no row"


def test_command_line_reports_created_replayed_conflict_and_missing(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    draft = create_draft(sandbox)
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", sandbox.writer_url)
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", sandbox.schema)
    arguments = [
        "--binding-id",
        draft.binding_id,
        "--revision",
        draft.revision,
        "--revision-id",
        draft.revision_id,
        "--capacity-manifest-id",
        CAPACITY,
        "--qualification-report-id",
        REPORT,
    ]

    main(arguments)
    assert "created=true" in capsys.readouterr().out

    main(arguments)
    assert "created=false" in capsys.readouterr().out
    assert counts(sandbox) == (1, 1, 1)

    with pytest.raises(SystemExit) as conflict:
        main([*arguments[:5], REPLACEMENT_REVISION_ID, *arguments[6:]])
    assert conflict.value.code == EXIT_CONFLICT
    assert capsys.readouterr().err.startswith("conflict:")

    with pytest.raises(SystemExit) as missing:
        main([*arguments[:3], str(int(draft.revision) + 1), *arguments[4:]])
    assert missing.value.code == EXIT_NOT_STORED
    assert "no row" in capsys.readouterr().err

    assert PostgresPublicJobs(sandbox.pool).latest_binding(draft.binding_id) is not None
    assert counts(sandbox) == (1, 1, 1)


def test_publish_records_capacity_and_qualification_evidence(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)

    result = publish(sandbox, draft)

    row = sandbox.admin.execute(
        f"SELECT object_id, revision, revision_id, capacity_manifest_id, "
        f"qualification_report_id, evidence_source "
        f'FROM "{sandbox.schema}".speech_binding_publication_evidence'
    ).fetchone()
    assert row == (draft.binding_id, "1", draft.revision_id, CAPACITY, REPORT, "operator_cli")
    assert result.evidence.evidence_source == "operator_cli"
    assert result.evidence.capacity_manifest_id == CAPACITY
    assert result.evidence.qualification_report_id == REPORT


def test_capacity_manifest_mismatch_is_rejected_before_insert(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)

    with pytest.raises(BindingCapacityManifestMismatchError) as failure:
        publish(sandbox, draft, capacity_manifest_id="cap-other-0001")

    assert CAPACITY in str(failure.value)
    assert "cap-other-0001" in str(failure.value)
    assert counts(sandbox) == (0, 1, 0), "an evidence mismatch must not publish"


def test_replay_with_different_evidence_is_rejected(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    publish(sandbox, draft)

    with pytest.raises(BindingEvidenceConflictError):
        publish(sandbox, draft, qualification_report_id="qrep-other-0001")

    assert counts(sandbox) == (1, 1, 1), "different evidence must not overwrite the row"


def test_missing_evidence_arguments_write_nothing(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    draft = create_draft(sandbox)
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", sandbox.writer_url)
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", sandbox.schema)
    arguments = [
        "--binding-id",
        draft.binding_id,
        "--revision",
        draft.revision,
        "--revision-id",
        draft.revision_id,
        "--capacity-manifest-id",
        CAPACITY,
    ]

    with pytest.raises(SystemExit) as stopped:
        main(arguments)

    assert stopped.value.code == 2
    assert "--qualification-report-id" in capsys.readouterr().err
    assert counts(sandbox) == (0, 1, 0)
