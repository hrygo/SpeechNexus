"""Real PostgreSQL proof for publication evidence, honest GET and backfill.

Synthetic identities prove storage, projection and permission semantics only;
they do not qualify a production deployment, real speech engine or read replica.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command as migration
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from speechnexus_contracts.model_bindings import parse as parse_binding
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.backfill_binding_evidence import main as backfill_main
from speechnexus_backend.publish_binding import publish_binding
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.binding_publication import BindingPublishIntent
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import (
    PostgresBindingDrafts,
)
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    TABLE as EVIDENCE,
)
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    binding_admin_reader_grants,
    publication_evidence_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
    publication_grants,
)
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_publication_evidence"
PATH = "/v1/nexus/admin/model-bindings"
CAPACITY = "cap-asr-quality-7"
REPORT = "qrep-asr-quality-7"
ROLES = ("writer", "publication", "reader", "catalogue")


@dataclass
class Sandbox:
    admin: psycopg.Connection[tuple[object, ...]]
    schema: str
    urls: dict[str, str]
    pools: dict[str, SpeechPool]


@pytest.fixture
def sandbox(request: pytest.FixtureRequest) -> Iterator[Sandbox]:
    base = os.environ.get(VARIABLE)
    if not base:
        pytest.skip("Configure an isolated PostgreSQL DSN; skip is not evidence")
    suffix = uuid.uuid4().hex[:10]
    schema = "sn_evi_" + suffix
    names = {role: f"sn_evi_{role}_{suffix}" for role in ROLES}
    engine = create_engine(
        base.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    legacy_migration_test = (
        request.node.name == "test_downgrade_refuses_nonempty_evidence_and_preserves_publications"
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(
                config, "0021_publication_evidence" if legacy_migration_test else "head"
            )
    finally:
        engine.dispose()
    admin = psycopg.connect(base, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    for role in ROLES:
        admin.execute(f"CREATE ROLE \"{names[role]}\" LOGIN PASSWORD '{PASSWORD}'")
    grants = (
        (names["writer"], binding_draft_grants),
        (names["publication"], binding_draft_grants),
        (names["publication"], publication_grants),
        (names["publication"], publication_evidence_grants),
        (names["reader"], binding_admin_reader_grants),
        (names["catalogue"], model_catalog_reader_grants),
    )
    if legacy_migration_test:
        # This test only writes drafts and exercises the 0021 downgrade.
        # Later runtime grants refer to tables introduced after that revision.
        grants = ((names["writer"], binding_draft_grants),)
    for role, declaration in grants:
        for statement in declaration(schema, role):
            admin.execute(statement)
    urls = {role: make_conninfo(base, user=names[role], password=PASSWORD) for role in ROLES}
    pools: dict[str, SpeechPool] = {}
    for role in ROLES:
        pool = speech_pool(urls[role], schema=schema, max_size=2, name=f"evi-{role}-" + suffix)
        pool.open()
        pools[role] = pool
    try:
        yield Sandbox(admin, schema, urls, pools)
    finally:
        for pool in pools.values():
            pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in ROLES:
            admin.execute(f'DROP ROLE IF EXISTS "{names[role]}"')
        admin.close()


def settings(sandbox: Sandbox) -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "database_schema": sandbox.schema,
            "binding_writer_database_url": sandbox.urls["writer"],
            "binding_admin_reader_database_url": sandbox.urls["reader"],
            "asset_allowed_hosts": ("testserver", "test", "127.0.0.1"),
            "binding_receive_concurrency": 1,
            "binding_transaction_timeout_seconds": 2,
        }
    )


def fixed(body: dict[str, Any], revision: str = "1") -> str:
    return PATH + f"/{body['binding_id']}/revisions/{revision}"


def create_draft(sandbox: Sandbox, key: str, *, binding_id: str = "asr-quality") -> BindingDraft:
    body = {**binding_request(), "binding_id": binding_id}
    result = PostgresBindingDrafts(sandbox.pools["writer"]).create(prepare_binding_draft(body, key))
    assert result.outcome == "created", result
    assert result.draft is not None
    return result.draft


def add_legacy_publication(sandbox: Sandbox, draft: BindingDraft) -> None:
    """Simulate a B-era publication written without evidence."""
    sandbox.admin.execute(
        f'INSERT INTO "{sandbox.schema}".speech_publications '
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('binding', %s, %s, %s, 1700000000000000)",
        (draft.binding_id, draft.revision, draft.revision_id),
    )


def counts(sandbox: Sandbox) -> tuple[int, int, int]:
    row = sandbox.admin.execute(
        f'SELECT (SELECT count(*) FROM "{sandbox.schema}".speech_publications), '
        f'(SELECT count(*) FROM "{sandbox.schema}".speech_binding_revisions), '
        f'(SELECT count(*) FROM "{sandbox.schema}".{EVIDENCE})'
    ).fetchone()
    assert row is not None
    publications, revisions, evidence = row
    assert isinstance(publications, int) and isinstance(revisions, int)
    assert isinstance(evidence, int)
    return publications, revisions, evidence


def publish_intent(sandbox: Sandbox, binding_id: str, key: str) -> BindingPublishIntent:
    observation = PostgresPublications(sandbox.pools["publication"]).observe_binding_head(
        binding_id
    )
    return BindingPublishIntent.from_request(
        installation_id=str(observation["installation_id"]),
        database=str(observation["database_name"]),
        schema=sandbox.schema,
        expected_current_revision=None,
        expected_catalog_revision=int(observation["catalog_revision"]),
        idempotency_key=key,
    )


def connection(sandbox: Sandbox, role: str) -> psycopg.Connection[tuple[object, ...]]:
    conn = psycopg.connect(sandbox.urls[role], autocommit=True)
    conn.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
    return conn


def test_get_reports_draft_published_and_unavailable(sandbox: Sandbox) -> None:
    body = binding_request()
    with TestClient(create_app(settings=settings(sandbox))) as client:
        created = client.post(PATH, json=body, headers={"Idempotency-Key": "evidence-http-key"})
        assert created.status_code == 201, created.text
        created_wire = created.json()
        draft = PostgresBindingDrafts(sandbox.pools["writer"]).get(body["binding_id"], "1")
        assert draft is not None

        draft_wire = client.get(fixed(body)).json()
        assert draft_wire["publication"] == created_wire["publication"]
        assert draft_wire["publication"]["status"] == "draft"
        assert draft_wire["publication"]["capacity_manifest_id"] is None

        result = publish_binding(
            database_url=sandbox.urls["publication"],
            schema=sandbox.schema,
            binding_id=draft.binding_id,
            revision=int(draft.revision),
            revision_id=draft.revision_id,
            capacity_manifest_id=CAPACITY,
            qualification_report_id=REPORT,
            expected_current_revision=None,
            intent=publish_intent(sandbox, draft.binding_id, "evidence-test"),
        )
        assert result.created is True

        published = client.get(fixed(body))
        assert published.status_code == 200, published.text
        wire = published.json()
        parse_binding("ModelBindingRevision", wire)
        assert wire["publication"]["status"] == "published"
        assert wire["publication"]["capacity_manifest_id"] == CAPACITY
        assert wire["publication"]["qualification_report_id"] == REPORT
        assert wire["publication"]["published_at"].endswith("Z")
        assert wire["publication"]["retired_at"] is None
        assert wire["publication"]["disabled_reason"] is None

        legacy = {**binding_request(), "binding_id": "legacy-binding"}
        legacy_created = client.post(
            PATH, json=legacy, headers={"Idempotency-Key": "legacy-http-key"}
        )
        assert legacy_created.status_code == 201, legacy_created.text
        legacy_draft = PostgresBindingDrafts(sandbox.pools["writer"]).get("legacy-binding", "1")
        assert legacy_draft is not None
        add_legacy_publication(sandbox, legacy_draft)

        unavailable = client.get(fixed(legacy))
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "binding_unavailable"

    jobs = PostgresPublicJobs(sandbox.pools["publication"])
    assert jobs.latest_binding("legacy-binding") is None, (
        "public resolution must not select a publication that admin GET cannot project"
    )
    assert counts(sandbox) == (2, 2, 1)


def test_writer_reader_and_catalogue_permissions_are_narrow(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox, "permission-key")
    add_legacy_publication(sandbox, draft)
    evidence_sql = (
        f'INSERT INTO "{sandbox.schema}".{EVIDENCE} '
        "(kind, object_id, revision, revision_id, capacity_manifest_id, "
        "qualification_report_id, evidence_source, recorded_at_us) "
        "VALUES ('binding', %s, %s, %s, %s, %s, 'backfill', 1)"
    )
    arguments = (draft.binding_id, draft.revision, draft.revision_id, CAPACITY, REPORT)

    writer = connection(sandbox, "writer")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute(f'SELECT count(*) FROM "{sandbox.schema}".speech_publications')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute(evidence_sql, arguments)
    finally:
        writer.close()

    reader = connection(sandbox, "reader")
    try:
        reader.execute(f'SELECT count(*) FROM "{sandbox.schema}".{EVIDENCE}').fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute(evidence_sql, arguments)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute(
                f'INSERT INTO "{sandbox.schema}".speech_publications '
                "(kind, object_id, revision, revision_id, published_at_us) "
                "VALUES ('binding', 'reader-attempt', '1', 'mbrev-reader-0001', 1)"
            )
    finally:
        reader.close()

    catalogue = connection(sandbox, "catalogue")
    try:
        catalogue.execute(f'SELECT count(*) FROM "{sandbox.schema}".speech_publications').fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            catalogue.execute(f'SELECT count(*) FROM "{sandbox.schema}".{EVIDENCE}').fetchone()
    finally:
        catalogue.close()

    assert counts(sandbox) == (1, 1, 0)


def test_backfill_preflight_dry_run_apply_idempotent_conflict_and_rollback(
    sandbox: Sandbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = create_draft(sandbox, "backfill-first")
    add_legacy_publication(sandbox, first)
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", sandbox.urls["publication"])
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", sandbox.schema)

    with pytest.raises(SystemExit) as blocked:
        backfill_main(["--preflight"])
    assert blocked.value.code == 10
    assert "preflight publications=1" in capsys.readouterr().out

    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            [
                {
                    "object_id": first.binding_id,
                    "revision": first.revision,
                    "revision_id": first.revision_id,
                    "capacity_manifest_id": CAPACITY,
                    "qualification_report_id": REPORT,
                }
            ]
        )
    )
    backfill_main(["--dry-run", "--mapping", str(mapping)])
    dry = capsys.readouterr().out
    assert "insert=1" in dry and "unresolved_after=0" in dry

    backfill_main(["--apply", "--mapping", str(mapping)])
    applied = capsys.readouterr().out
    assert "inserted=1" in applied and "evidence_after=1" in applied
    assert counts(sandbox) == (1, 1, 1)

    backfill_main(["--apply", "--mapping", str(mapping)])
    replay = capsys.readouterr().out
    assert "inserted=0" in replay and "skipped=1" in replay
    assert "evidence_before=1" in replay and "evidence_after=1" in replay
    assert counts(sandbox) == (1, 1, 1)

    backfill_main(["--preflight"])
    assert "unresolved=0" in capsys.readouterr().out

    conflicting = tmp_path / "conflicting.json"
    conflicting.write_text(
        json.dumps(
            [
                {
                    "object_id": first.binding_id,
                    "revision": first.revision,
                    "revision_id": first.revision_id,
                    "capacity_manifest_id": CAPACITY,
                    "qualification_report_id": "qrep-other-0001",
                }
            ]
        )
    )
    with pytest.raises(SystemExit) as conflict:
        backfill_main(["--apply", "--mapping", str(conflicting)])
    assert conflict.value.code == 11
    assert counts(sandbox) == (1, 1, 1)

    second = create_draft(sandbox, "backfill-second", binding_id="second-binding")
    third = create_draft(sandbox, "backfill-third", binding_id="third-binding")
    add_legacy_publication(sandbox, second)
    add_legacy_publication(sandbox, third)
    rollback = tmp_path / "rollback.json"
    rollback.write_text(
        json.dumps(
            [
                {
                    "object_id": second.binding_id,
                    "revision": second.revision,
                    "revision_id": second.revision_id,
                    "capacity_manifest_id": CAPACITY,
                    "qualification_report_id": REPORT,
                },
                {
                    "object_id": third.binding_id,
                    "revision": third.revision,
                    "revision_id": third.revision_id,
                    "capacity_manifest_id": "cap-wrong-0001",
                    "qualification_report_id": REPORT,
                },
            ]
        )
    )
    with pytest.raises(SystemExit) as rejected:
        backfill_main(["--apply", "--mapping", str(rollback)])
    assert rejected.value.code == 11
    assert counts(sandbox) == (3, 3, 1), "a later mapping failure must roll back earlier inserts"


def test_backfill_export_preserves_recorded_evidence(
    sandbox: Sandbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    draft = create_draft(sandbox, "export-key")
    add_legacy_publication(sandbox, draft)
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", sandbox.urls["publication"])
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", sandbox.schema)
    mapping = tmp_path / "export.json"
    mapping.write_text(
        json.dumps(
            [
                {
                    "object_id": draft.binding_id,
                    "revision": draft.revision,
                    "revision_id": draft.revision_id,
                    "capacity_manifest_id": CAPACITY,
                    "qualification_report_id": REPORT,
                }
            ]
        )
    )
    backfill_main(["--apply", "--mapping", str(mapping)])
    capsys.readouterr()

    backfill_main(["--export"])

    exported = json.loads(capsys.readouterr().out)
    assert exported[0]["capacity_manifest_id"] == CAPACITY
    assert exported[0]["qualification_report_id"] == REPORT
    assert exported[0]["evidence_source"] == "backfill"


def test_downgrade_refuses_nonempty_evidence_and_preserves_publications(
    sandbox: Sandbox,
) -> None:
    draft = create_draft(sandbox, "downgrade-key")
    add_legacy_publication(sandbox, draft)
    sandbox.admin.execute(
        f'INSERT INTO "{sandbox.schema}".{EVIDENCE} '
        "(kind, object_id, revision, revision_id, capacity_manifest_id, "
        "qualification_report_id, evidence_source, recorded_at_us) "
        "VALUES ('binding', %s, %s, %s, %s, %s, 'operator_cli', 1)",
        (draft.binding_id, draft.revision, draft.revision_id, CAPACITY, REPORT),
    )
    before = counts(sandbox)
    assert before == (1, 1, 1)
    engine = create_engine(
        os.environ[VARIABLE].replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = sandbox.schema
    try:
        with engine.connect() as db:
            config.attributes["connection"] = db
            with pytest.raises(RuntimeError, match="export and preservation"):
                migration.downgrade(config, "0020_artifacts")
    finally:
        engine.dispose()

    sandbox.admin.execute(f'DELETE FROM "{sandbox.schema}".{EVIDENCE}')
    engine = create_engine(
        os.environ[VARIABLE].replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as db:
            config.attributes["connection"] = db
            migration.downgrade(config, "0020_artifacts")
    finally:
        engine.dispose()

    remaining = sandbox.admin.execute(
        "SELECT to_regclass(%s)", (f"{sandbox.schema}.{EVIDENCE}",)
    ).fetchone()
    assert remaining is not None and remaining[0] is None
    kept = sandbox.admin.execute(
        f'SELECT (SELECT count(*) FROM "{sandbox.schema}".speech_publications), '
        f'(SELECT count(*) FROM "{sandbox.schema}".speech_binding_revisions)'
    ).fetchone()
    assert kept == (1, 1), "downgrade must not touch publication or revision rows"
