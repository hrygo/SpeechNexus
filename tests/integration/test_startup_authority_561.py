"""Real-role startup authority for the five #561 runtimes; no skip counts as evidence."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_public_jobs_http import (
    ROOT,
    VARIABLE,
    JobSandbox,
    _body,
    _seed_binding,
)
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.artifact_runtime import ArtifactRuntime, _verify_read_only
from speechnexus_backend.binding_runtime import BindingAdminReaderRuntime, BindingRuntime
from speechnexus_backend.job_runtime import JobRuntime, _verify_privileges
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.infrastructure.artifact_schema import (
    artifact_publisher_grants,
    artifact_reader_grants,
)
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    binding_admin_reader_grants,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_not_submitted_grants,
    cancellation_reconciliation_grants,
    job_cancellation_fence_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    JOB_WRITER_INSERT_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.voice_draft_schema import voice_draft_grants
from speechnexus_backend.speech.interfaces.http.binding_ingress import CREATE_PATH
from speechnexus_backend.workbench.infrastructure.runtime_schema import (
    bootstrap_sql,
    runtime_grants_sql,
)
from speechnexus_backend.workbench.runtime import RuntimeStartupError, check_database
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings

pytestmark = pytest.mark.integration

ROLE_PASSWORD = "synthetic_startup_authority"
SUBJECT_PREFIX = "sn561_"


@pytest.fixture
def jobs_db() -> Iterator[JobSandbox]:
    """One freshly migrated schema with the documented minimum Job writer role."""
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not Job evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_jobs_" + suffix, "sn_jobs_role_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    pool = None
    try:
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{ROLE_PASSWORD}'")
        for grant in (
            *job_runtime_grants(schema, role),
            *public_job_grants(schema, role),
            *voice_draft_grants(schema, role),
            *job_dispatch_grants(schema, role),
            *cancellation_reconciliation_grants(schema, role),
            *public_job_submission_grants(schema, role),
            f'GRANT SELECT ON "{schema}".admission_operations, "{schema}".coordination_jobs '
            f'TO "{role}"',
            f"GRANT UPDATE (claim_owner_id, claim_expires_at_us, claim_version, job_state) "
            f'ON "{schema}".coordination_jobs TO "{role}"',
            f"GRANT SELECT ON speech_workspaces, speech_publications, "
            f"speech_publication_heads, speech_catalog_state, speech_publication_retirements, "
            f"speech_binding_revisions, speech_assets, speech_pronunciation_revisions "
            f'TO "{role}"',
        ):
            admin.execute(grant)
        _seed_binding(admin)
        pool = speech_pool(
            make_conninfo(url, user=role, password=ROLE_PASSWORD),
            schema=schema,
            max_size=2,
            name="jobs-" + suffix,
        )
        pool.open()
        adapter = PostgresPublicJobs(pool, timeout_seconds=3)
        yield JobSandbox(url, schema, role, admin, PublicJobs(adapter, adapter), pool)
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def _create_role(db: JobSandbox, prefix: str) -> str:
    role = SUBJECT_PREFIX + prefix + "_" + uuid.uuid4().hex[:8]
    db.admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{ROLE_PASSWORD}'")
    return role


def _drop_role(db: JobSandbox, role: str) -> None:
    db.admin.execute(f'DROP OWNED BY "{role}"')
    db.admin.execute(f'DROP ROLE IF EXISTS "{role}"')


def _connect(db: JobSandbox, role: str) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(
        make_conninfo(db.url, user=role, password=ROLE_PASSWORD), row_factory=dict_row
    )


def _artifact_settings(db: JobSandbox, role: str) -> Settings:
    return _settings().model_copy(
        update={
            "database_schema": db.schema,
            "artifact_reader_database_url": SecretStr(
                make_conninfo(db.url, user=role, password=ROLE_PASSWORD)
            ),
            "object_storage_access_key": SecretStr("synthetic-access"),
            "object_storage_secret_key": SecretStr("synthetic-secret"),
        }
    )


def _job_settings(db: JobSandbox) -> Settings:
    return _settings().model_copy(
        update={
            "database_schema": db.schema,
            "job_writer_database_url": SecretStr(
                make_conninfo(db.url, user=db.role, password=ROLE_PASSWORD)
            ),
        }
    )


def _binding_settings(db: JobSandbox, *, writer: str | None, reader: str | None) -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "database_schema": db.schema,
            "binding_writer_database_url": None
            if writer is None
            else SecretStr(make_conninfo(db.url, user=writer, password=ROLE_PASSWORD)),
            "binding_admin_reader_database_url": None
            if reader is None
            else SecretStr(make_conninfo(db.url, user=reader, password=ROLE_PASSWORD)),
            "binding_receive_concurrency": 1,
            "binding_transaction_timeout_seconds": 1,
            "asset_allowed_hosts": ("testserver", "test", "127.0.0.1"),
        }
    )


# Each row is one authority the reader identity must never hold. The grants keep a
# single extra privilege on top of ``artifact_reader_grants`` so a column predicate
# cannot hide behind a granted table privilege.
ARTIFACT_MISGRANTS = (
    (
        "table-references",
        'GRANT REFERENCES ON "{schema}".speech_artifacts TO "{role}"',
        "must not hold REFERENCES on speech_artifacts",
    ),
    (
        "table-trigger",
        'GRANT TRIGGER ON "{schema}".speech_artifacts TO "{role}"',
        "must not hold TRIGGER on speech_artifacts",
    ),
    (
        "column-insert",
        'GRANT INSERT (media_type) ON "{schema}".speech_artifacts TO "{role}"',
        "must not hold column INSERT on speech_artifacts",
    ),
    (
        "column-update",
        'GRANT UPDATE (media_type) ON "{schema}".speech_artifacts TO "{role}"',
        "must not hold column UPDATE on speech_artifacts",
    ),
    (
        "column-references",
        'GRANT REFERENCES (media_type) ON "{schema}".speech_artifacts TO "{role}"',
        "must not hold column REFERENCES on speech_artifacts",
    ),
    (
        "undeclared-table",
        'GRANT SELECT ON "{schema}".speech_jobs TO "{role}"',
        "undeclared table speech_jobs",
    ),
    (
        "undeclared-column",
        'GRANT SELECT (job_id) ON "{schema}".speech_jobs TO "{role}"',
        "undeclared table speech_jobs",
    ),
    (
        "schema-create",
        'GRANT CREATE ON SCHEMA "{schema}" TO "{role}"',
        "must not hold CREATE on schema",
    ),
)


@pytest.mark.parametrize(
    ("grant", "detail"),
    [(grant, detail) for _, grant, detail in ARTIFACT_MISGRANTS],
    ids=[case for case, _, _ in ARTIFACT_MISGRANTS],
)
def test_artifact_reader_startup_rejects_each_single_misgrant(
    jobs_db: JobSandbox, grant: str, detail: str
) -> None:
    role = _create_role(jobs_db, "art_bad")
    try:
        for statement in artifact_reader_grants(jobs_db.schema, role):
            jobs_db.admin.execute(statement)
        jobs_db.admin.execute(grant.format(schema=jobs_db.schema, role=role))
        with _connect(jobs_db, role) as connection, pytest.raises(ValueError, match=detail):
            _verify_read_only(connection, jobs_db.schema)
        runtime = ArtifactRuntime(_artifact_settings(jobs_db, role))
        with pytest.raises(ValueError, match="Artifact reader startup rejected"):
            runtime.open()
    finally:
        _drop_role(jobs_db, role)


def test_artifact_reader_schema_create_message_is_complete(jobs_db: JobSandbox) -> None:
    role = _create_role(jobs_db, "art_msg")
    try:
        for statement in artifact_reader_grants(jobs_db.schema, role):
            jobs_db.admin.execute(statement)
        jobs_db.admin.execute(f'GRANT CREATE ON SCHEMA "{jobs_db.schema}" TO "{role}"')
        with _connect(jobs_db, role) as connection, pytest.raises(ValueError) as caught:
            _verify_read_only(connection, jobs_db.schema)
        assert str(caught.value) == (
            f"Artifact reader must not hold CREATE on schema {jobs_db.schema}; "
            "revoke the schema CREATE grant"
        )
    finally:
        _drop_role(jobs_db, role)


def test_artifact_reader_minimum_role_reads_one_artifact(jobs_db: JobSandbox) -> None:
    role = _create_role(jobs_db, "art_ok")
    artifact_id = "artifact-561-" + uuid.uuid4().hex[:8]
    try:
        for statement in artifact_reader_grants(jobs_db.schema, role):
            jobs_db.admin.execute(statement)
        jobs_db.admin.execute(
            "INSERT INTO speech_artifacts (artifact_id, workspace_id, artifact_kind, "
            "artifact_state, media_type, bytes, sha256, object_key, storage_version_id, "
            "created_at_us) VALUES (%s, 'workspace-default', 'transcript', 'ready', "
            "'text/plain', 12, %s, %s, 'synthetic-version', 1700000000000000)",
            (artifact_id, "sha256:" + "a" * 64, "private/artifacts/" + artifact_id),
        )
        with _connect(jobs_db, role) as connection:
            _verify_read_only(connection, jobs_db.schema)
        runtime = ArtifactRuntime(_artifact_settings(jobs_db, role))
        try:
            runtime.open()
            assert runtime.service is not None
            record = runtime.service.metadata(artifact_id)
            assert record.artifact_id == artifact_id
            assert record.workspace_id == "workspace-default"
        finally:
            runtime.close()
    finally:
        _drop_role(jobs_db, role)


# The Job writer role is the fixture's documented minimum. One extra authority is
# layered on it per case, so the baseline grants never mask the rejected privilege.
JOB_MISGRANTS = (
    (
        "writable-table-references",
        'GRANT REFERENCES ON "{schema}".speech_jobs TO "{role}"',
        "must not hold REFERENCES on speech_jobs",
    ),
    (
        "writable-table-trigger",
        'GRANT TRIGGER ON "{schema}".speech_jobs TO "{role}"',
        "must not hold TRIGGER on speech_jobs",
    ),
    (
        "read-only-table-insert",
        'GRANT INSERT ON "{schema}".speech_workspaces TO "{role}"',
        "must not hold INSERT on speech_workspaces",
    ),
    (
        "column-writable-table-insert",
        'GRANT INSERT ON "{schema}".speech_artifacts TO "{role}"',
        "must not hold INSERT on speech_artifacts",
    ),
    (
        "coordination-table-insert",
        'GRANT INSERT ON "{schema}".coordination_jobs TO "{role}"',
        "must not hold INSERT on coordination_jobs",
    ),
    (
        "admission-table-insert",
        'GRANT INSERT ON "{schema}".admission_operations TO "{role}"',
        "must not hold INSERT on admission_operations",
    ),
    (
        "reconciliation-table-insert",
        'GRANT INSERT ON "{schema}".speech_job_cancellation_reconciliations TO "{role}"',
        "must not hold INSERT on speech_job_cancellation_reconciliations",
    ),
    (
        "artifact-missing-insert-column",
        'REVOKE INSERT (media_type) ON "{schema}".speech_artifacts FROM "{role}"',
        "INSERT columns do not match the declared surface on speech_artifacts",
    ),
    (
        "column-writable-column-references",
        'GRANT REFERENCES (artifact_id) ON "{schema}".speech_artifacts TO "{role}"',
        "must not hold column REFERENCES on speech_artifacts",
    ),
    (
        "read-only-column-insert",
        'GRANT INSERT (workspace_id) ON "{schema}".speech_workspaces TO "{role}"',
        "must not hold column INSERT on speech_workspaces",
    ),
    (
        "read-only-column-update",
        'GRANT UPDATE (display_name) ON "{schema}".speech_workspaces TO "{role}"',
        "UPDATE columns do not match the declared surface on speech_workspaces",
    ),
    (
        "read-only-column-references",
        'GRANT REFERENCES (workspace_id) ON "{schema}".speech_workspaces TO "{role}"',
        "must not hold column REFERENCES on speech_workspaces",
    ),
    (
        "read-only-table-references",
        'GRANT REFERENCES ON "{schema}".speech_workspaces TO "{role}"',
        "must not hold REFERENCES on speech_workspaces",
    ),
    (
        "declared-extra-table-delete",
        'GRANT DELETE ON "{schema}".coordination_jobs TO "{role}"',
        "must not hold DELETE on coordination_jobs",
    ),
    (
        "writable-table-update",
        'GRANT UPDATE ON "{schema}".speech_jobs TO "{role}"',
        "UPDATE columns do not match the declared surface on speech_jobs",
    ),
    (
        "writable-extra-column-update",
        'GRANT UPDATE (status) ON "{schema}".speech_voice_drafts TO "{role}"',
        "UPDATE columns do not match the declared surface on speech_voice_drafts",
    ),
    (
        "append-only-table-update",
        'GRANT UPDATE ON "{schema}".speech_job_events TO "{role}"',
        "UPDATE columns do not match the declared surface on speech_job_events",
    ),
    (
        "undeclared-table",
        'GRANT SELECT ON "{schema}".speech_pronunciation_families TO "{role}"',
        "undeclared table speech_pronunciation_families",
    ),
    (
        "schema-create",
        'GRANT CREATE ON SCHEMA "{schema}" TO "{role}"',
        "must not hold CREATE on schema",
    ),
    (
        "missing-schema-usage",
        'REVOKE USAGE ON SCHEMA "{schema}" FROM "{role}"',
        "lacks USAGE on schema",
    ),
)


@pytest.mark.parametrize(
    ("grant", "detail"),
    [(grant, detail) for _, grant, detail in JOB_MISGRANTS],
    ids=[case for case, _, _ in JOB_MISGRANTS],
)
def test_job_writer_startup_rejects_each_single_misgrant(
    jobs_db: JobSandbox, grant: str, detail: str
) -> None:
    jobs_db.admin.execute(grant.format(schema=jobs_db.schema, role=jobs_db.role))
    with _connect(jobs_db, jobs_db.role) as connection, pytest.raises(ValueError, match=detail):
        _verify_privileges(connection, jobs_db.schema)
    runtime = JobRuntime(_job_settings(jobs_db))
    with pytest.raises(ValueError, match="Public Job writer startup rejected"):
        runtime.open()
    assert runtime.jobs is None


def _apply_job_writer_grants(jobs_db: JobSandbox, role: str) -> None:
    for statement in (
        *job_runtime_grants(jobs_db.schema, role),
        *job_dispatch_grants(jobs_db.schema, role),
        *public_job_grants(jobs_db.schema, role),
        *voice_draft_grants(jobs_db.schema, role),
        *public_job_submission_grants(jobs_db.schema, role),
        *job_cancellation_fence_grants(jobs_db.schema, role),
        *cancellation_not_submitted_grants(jobs_db.schema, role),
        *cancellation_reconciliation_grants(jobs_db.schema, role),
        *artifact_publisher_grants(jobs_db.schema, role),
    ):
        jobs_db.admin.execute(statement)


def _apply_job_writer_baseline(jobs_db: JobSandbox, role: str) -> None:
    """Grant functions plus the read/UPDATE projection already provisioned for the
    combined writer; the README migration only replaces the INSERT surface."""
    _apply_job_writer_grants(jobs_db, role)
    for statement in (
        f'GRANT SELECT ON "{jobs_db.schema}".admission_operations, '
        f'"{jobs_db.schema}".coordination_jobs TO "{role}"',
        f"GRANT UPDATE (claim_owner_id, claim_expires_at_us, claim_version, job_state) "
        f'ON "{jobs_db.schema}".coordination_jobs TO "{role}"',
        f'GRANT SELECT ON "{jobs_db.schema}".speech_workspaces, '
        f'"{jobs_db.schema}".speech_publications, '
        f'"{jobs_db.schema}".speech_publication_heads, '
        f'"{jobs_db.schema}".speech_catalog_state, '
        f'"{jobs_db.schema}".speech_publication_retirements, '
        f'"{jobs_db.schema}".speech_binding_revisions, '
        f'"{jobs_db.schema}".speech_assets, '
        f'"{jobs_db.schema}".speech_pronunciation_revisions TO "{role}"',
    ):
        jobs_db.admin.execute(statement)


def test_documented_upgrade_migration_restores_the_job_writer(
    jobs_db: JobSandbox,
) -> None:
    role = _create_role(jobs_db, "job_upgrade")
    tables = tuple(JOB_WRITER_INSERT_COLUMNS)
    try:
        _apply_job_writer_baseline(jobs_db, role)
        # Rebuild the pre-upgrade shape: table-level INSERT, no column grants.
        for table in tables:
            rows = jobs_db.query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (jobs_db.schema, table),
            )
            columns = ", ".join(f'"{row["column_name"]}"' for row in rows)
            jobs_db.admin.execute(f'REVOKE INSERT ON "{jobs_db.schema}".{table} FROM "{role}"')
            jobs_db.admin.execute(
                f'REVOKE INSERT ({columns}) ON "{jobs_db.schema}".{table} FROM "{role}"'
            )
            jobs_db.admin.execute(f'GRANT INSERT ON "{jobs_db.schema}".{table} TO "{role}"')
        with _connect(jobs_db, role) as connection, pytest.raises(ValueError) as caught:
            _verify_privileges(connection, jobs_db.schema)
        assert "must not hold INSERT on" in str(caught.value)
        assert "re-grant INSERT only on the declared columns" in str(caught.value)

        # The exact steps documented in deploy/README.md: revoke the table grants,
        # then re-apply the Job-writer grant functions (never the admission role).
        for table in tables:
            jobs_db.admin.execute(f'REVOKE INSERT ON "{jobs_db.schema}".{table} FROM "{role}"')
        _apply_job_writer_grants(jobs_db, role)
        with _connect(jobs_db, role) as connection:
            _verify_privileges(connection, jobs_db.schema)
    finally:
        _drop_role(jobs_db, role)


def test_job_writer_table_insert_message_names_the_regrant(
    jobs_db: JobSandbox,
) -> None:
    role = _create_role(jobs_db, "job_msg")
    try:
        _apply_job_writer_baseline(jobs_db, role)
        jobs_db.admin.execute(f'GRANT INSERT ON "{jobs_db.schema}".coordination_jobs TO "{role}"')
        with _connect(jobs_db, role) as connection, pytest.raises(ValueError) as caught:
            _verify_privileges(connection, jobs_db.schema)
        assert str(caught.value) == (
            "Public Job writer must not hold INSERT on coordination_jobs; "
            "revoke the table-level grant, then re-grant INSERT only on the declared "
            "columns; see deploy/README.md upgrade section"
        )
    finally:
        _drop_role(jobs_db, role)


def test_job_writer_writes_and_cancels_a_real_chain(jobs_db: JobSandbox) -> None:
    """Writer-surface creation/cancel chain; JobDispatcher claim/submit is covered by
    ``test_job_dispatch_pg.py`` and is not driven here."""
    runtime = JobRuntime(_job_settings(jobs_db))
    runtime.open()
    try:
        assert runtime.jobs is not None
    finally:
        runtime.close()
    app = create_app(settings=_job_settings(jobs_db), jobs=jobs_db.jobs)

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/nexus/jobs", json=_body(), headers={"Idempotency-Key": "synthetic-561-chain-1"}
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["id"]
            for table in (
                "coordination_jobs",
                "coordination_outbox",
                "speech_jobs",
                "speech_job_events",
                "speech_job_submissions",
                "speech_job_intents",
                "admission_operations",
            ):
                assert jobs_db.count(table) == 1, table
            jobs_db.jobs.advance(job_id, "running")
            succeeded = jobs_db.jobs.advance(
                job_id,
                "succeeded",
                result_json=(
                    '{"kind":"tts","audio_artifact_id":"artifact-synthetic",'
                    '"receipt_id":"receipt-synthetic"}'
                ),
            )
            assert succeeded.state == "succeeded"

            running = await client.post(
                "/v1/nexus/jobs", json=_body(), headers={"Idempotency-Key": "synthetic-561-chain-2"}
            )
            assert running.status_code == 202, running.text
            running_id = running.json()["id"]
            jobs_db.jobs.advance(running_id, "running")
            view = jobs_db.jobs.cancel(
                running_id, request_id="synthetic-cancel-2", idempotency_key=None
            )
            assert view.job.state == "cancelling"
            settled = jobs_db.jobs.settle_cancelled(
                running_id, expected_version=view.job.status_version
            )
            assert settled.state == "cancelled"

            pending = await client.post(
                "/v1/nexus/jobs", json=_body(), headers={"Idempotency-Key": "synthetic-561-chain-3"}
            )
            assert pending.status_code == 202, pending.text
            pending_id = pending.json()["id"]
            direct = jobs_db.jobs.cancel(
                pending_id, request_id="synthetic-cancel-3", idempotency_key=None
            )
            assert direct.job.state == "cancelled"

    asyncio.run(run())


def test_job_writer_rejects_extra_artifact_insert_column(jobs_db: JobSandbox) -> None:
    jobs_db.admin.execute(
        f'ALTER TABLE "{jobs_db.schema}".speech_artifacts ADD COLUMN extra_probe TEXT'
    )
    jobs_db.admin.execute(
        f'GRANT INSERT (extra_probe) ON "{jobs_db.schema}".speech_artifacts TO "{jobs_db.role}"'
    )
    with (
        _connect(jobs_db, jobs_db.role) as connection,
        pytest.raises(
            ValueError, match="INSERT columns do not match the declared surface on speech_artifacts"
        ),
    ):
        _verify_privileges(connection, jobs_db.schema)


def test_job_writer_without_the_draft_publication_view_fails_startup(
    jobs_db: JobSandbox,
) -> None:
    """A published draft is unreadable without the projection, so startup refuses.

    The view is the only place the draft reader can learn the published voice and
    the validation Job; without the grant ``getVoiceDraft`` cannot answer its
    declared 200 for a published draft (#519).
    """
    jobs_db.admin.execute(
        f'REVOKE SELECT ON "{jobs_db.schema}".speech_voice_draft_publications FROM "{jobs_db.role}"'
    )
    with (
        _connect(jobs_db, jobs_db.role) as connection,
        pytest.raises(ValueError, match="lacks SELECT on speech_voice_draft_publications"),
    ):
        _verify_privileges(connection, jobs_db.schema)
    runtime = JobRuntime(_job_settings(jobs_db))
    with pytest.raises(ValueError, match="Public Job writer startup rejected"):
        runtime.open()
    assert runtime.jobs is None


def test_job_writer_minimum_role_starts_and_creates_one_job(jobs_db: JobSandbox) -> None:
    runtime = JobRuntime(_job_settings(jobs_db))
    runtime.open()
    try:
        assert runtime.jobs is not None
    finally:
        runtime.close()
    headers = {"Idempotency-Key": "synthetic-561-job"}
    app = create_app(settings=_job_settings(jobs_db), jobs=jobs_db.jobs)

    async def create() -> Any:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/v1/nexus/jobs", json=_body(), headers=headers)

    created = asyncio.run(create())
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "queued"


BINDING_WRITER_MISGRANTS = (
    (
        "table-references",
        'GRANT REFERENCES ON "{schema}".speech_binding_revisions TO "{role}"',
        "must not hold REFERENCES on speech_binding_revisions",
    ),
    (
        "table-trigger",
        'GRANT TRIGGER ON "{schema}".speech_binding_revisions TO "{role}"',
        "must not hold TRIGGER on speech_binding_revisions",
    ),
    (
        "column-references",
        'GRANT REFERENCES (revision) ON "{schema}".speech_binding_revisions TO "{role}"',
        "must not hold column REFERENCES on speech_binding_revisions",
    ),
)


@pytest.mark.parametrize(
    ("grant", "detail"),
    [(grant, detail) for _, grant, detail in BINDING_WRITER_MISGRANTS],
    ids=[case for case, _, _ in BINDING_WRITER_MISGRANTS],
)
def test_binding_writer_startup_rejects_each_single_misgrant(
    jobs_db: JobSandbox, grant: str, detail: str
) -> None:
    role = _create_role(jobs_db, "bind_bad")
    try:
        for statement in binding_draft_grants(jobs_db.schema, role):
            jobs_db.admin.execute(statement)
        jobs_db.admin.execute(grant.format(schema=jobs_db.schema, role=role))
        runtime = BindingRuntime(_binding_settings(jobs_db, writer=role, reader=None))
        with pytest.raises(ValueError, match=detail), _connect(jobs_db, role) as connection:
            runtime._verify_role(connection, role)
        with pytest.raises(ValueError, match="Binding writer startup rejected"):
            runtime.open()
    finally:
        _drop_role(jobs_db, role)


BINDING_ADMIN_MISGRANTS = (
    (
        "table-insert",
        'GRANT INSERT ON "{schema}".speech_binding_revisions TO "{role}"',
        "must not hold INSERT on speech_binding_revisions",
    ),
    (
        "table-trigger",
        'GRANT TRIGGER ON "{schema}".speech_binding_revisions TO "{role}"',
        "must not hold TRIGGER on speech_binding_revisions",
    ),
    (
        "column-insert",
        'GRANT INSERT (revision) ON "{schema}".speech_binding_revisions TO "{role}"',
        "must not hold column INSERT on speech_binding_revisions",
    ),
    (
        "column-update",
        'GRANT UPDATE (revision) ON "{schema}".speech_binding_revisions TO "{role}"',
        "must not hold column UPDATE on speech_binding_revisions",
    ),
    (
        "column-references",
        'GRANT REFERENCES (revision) ON "{schema}".speech_binding_revisions TO "{role}"',
        "must not hold column REFERENCES on speech_binding_revisions",
    ),
    (
        "undeclared-table",
        'GRANT SELECT ON "{schema}".speech_jobs TO "{role}"',
        "undeclared table speech_jobs",
    ),
)


@pytest.mark.parametrize(
    ("grant", "detail"),
    [(grant, detail) for _, grant, detail in BINDING_ADMIN_MISGRANTS],
    ids=[case for case, _, _ in BINDING_ADMIN_MISGRANTS],
)
def test_binding_admin_reader_startup_rejects_each_single_misgrant(
    jobs_db: JobSandbox, grant: str, detail: str
) -> None:
    role = _create_role(jobs_db, "bind_admin_bad")
    try:
        for statement in binding_admin_reader_grants(jobs_db.schema, role):
            jobs_db.admin.execute(statement)
        jobs_db.admin.execute(grant.format(schema=jobs_db.schema, role=role))
        runtime = BindingAdminReaderRuntime(_binding_settings(jobs_db, writer=None, reader=role))
        with _connect(jobs_db, role) as connection, pytest.raises(ValueError, match=detail):
            runtime._verify_role(connection, role)
        with pytest.raises(ValueError, match="Binding admin reader startup rejected"):
            runtime.open()
    finally:
        _drop_role(jobs_db, role)


def test_binding_minimum_roles_create_and_read_one_draft(jobs_db: JobSandbox) -> None:
    writer = _create_role(jobs_db, "bind_ok")
    reader = _create_role(jobs_db, "bind_admin_ok")
    try:
        for statement in binding_draft_grants(jobs_db.schema, writer):
            jobs_db.admin.execute(statement)
        for statement in binding_admin_reader_grants(jobs_db.schema, reader):
            jobs_db.admin.execute(statement)
        settings = _binding_settings(jobs_db, writer=writer, reader=reader)
        app = create_app(settings=settings)
        body = binding_request()
        with TestClient(app) as client:
            created = client.post(
                CREATE_PATH, json=body, headers={"Idempotency-Key": "synthetic-561-binding"}
            )
            assert created.status_code == 201, created.text
            revision = created.json()["revision"]
            fixed = f"{CREATE_PATH}/{body['binding_id']}/revisions/{revision}"
            read = client.get(fixed)
            assert read.status_code == 200, read.text
            assert read.json()["binding_id"] == body["binding_id"]
            assert app.state.binding_runtime.provider is not None
            assert app.state.binding_admin_runtime.provider is not None
    finally:
        _drop_role(jobs_db, reader)
        _drop_role(jobs_db, writer)


@dataclass
class WorkbenchSandbox:
    url: str
    schema: str
    role: str
    admin: Any

    def settings(self) -> WorkbenchSettings:
        return WorkbenchSettings(
            database_url=SecretStr(make_conninfo(self.url, user=self.role, password=ROLE_PASSWORD)),
            database_schema=self.schema,
            workspace_id="workspace-runtime",
            deployment_id="synthetic-561",
            cursor_key="synthetic-32-byte-cursor-key-test-only",
        )


@pytest.fixture
def workbench_db(jobs_db: JobSandbox) -> Iterator[WorkbenchSandbox]:
    schema = "wb561_" + uuid.uuid4().hex[:10]
    role = _create_role(jobs_db, "wb")
    try:
        jobs_db.admin.execute(bootstrap_sql(schema))
        jobs_db.admin.execute(runtime_grants_sql(schema, role))
        yield WorkbenchSandbox(jobs_db.url, schema, role, jobs_db.admin)
    finally:
        jobs_db.admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        _drop_role(jobs_db, role)


WORKBENCH_MISGRANTS = (
    (
        "table-references",
        'GRANT REFERENCES ON "{schema}".projects TO "{role}"',
        "runtime role must not hold REFERENCES on projects",
    ),
    (
        "table-trigger",
        'GRANT TRIGGER ON "{schema}".projects TO "{role}"',
        "runtime role must not hold TRIGGER on projects",
    ),
    (
        "read-only-table-insert",
        'GRANT INSERT ON "{schema}".deletion_ledger TO "{role}"',
        "runtime role must not hold INSERT on deletion_ledger",
    ),
    (
        "read-only-column-insert",
        'GRANT INSERT (completed_at) ON "{schema}".deletion_ledger TO "{role}"',
        "runtime role must not hold column INSERT on deletion_ledger",
    ),
    (
        "read-only-column-update",
        'GRANT UPDATE (completed_at) ON "{schema}".deletion_ledger TO "{role}"',
        "runtime role must not hold column UPDATE on deletion_ledger",
    ),
    (
        "column-references",
        'GRANT REFERENCES (id) ON "{schema}".projects TO "{role}"',
        "runtime role must not hold column REFERENCES on projects",
    ),
)


@pytest.mark.parametrize(
    ("grant", "detail"),
    [(grant, detail) for _, grant, detail in WORKBENCH_MISGRANTS],
    ids=[case for case, _, _ in WORKBENCH_MISGRANTS],
)
async def test_workbench_startup_rejects_each_single_misgrant(
    workbench_db: WorkbenchSandbox, grant: str, detail: str
) -> None:
    workbench_db.admin.execute(grant.format(schema=workbench_db.schema, role=workbench_db.role))
    with pytest.raises(RuntimeStartupError) as caught:
        await check_database(workbench_db.settings())
    message = str(caught.value)
    assert message.startswith("Workbench runtime database is unavailable or unsafe.")
    if detail:
        assert detail in message
    assert workbench_db.schema not in message


async def test_workbench_minimum_role_starts_and_creates_one_project(
    workbench_db: WorkbenchSandbox,
) -> None:
    settings = workbench_db.settings()
    await check_database(settings)
    async with await psycopg.AsyncConnection.connect(
        settings.database_url.get_secret_value(), autocommit=False
    ) as connection:
        async with connection.transaction():
            await connection.execute(
                f'INSERT INTO "{workbench_db.schema}".projects '
                "(id, workspace_id, created_at, metadata_revision) "
                "VALUES ('project-561', 'workspace-runtime', now(), 1)"
            )
            await connection.execute(
                f'INSERT INTO "{workbench_db.schema}".project_versions '
                "(project_id, revision, name, description, status, updated_at) "
                "VALUES ('project-561', 1, 'synthetic project', NULL, 'active', now())"
            )
        row = await (
            await connection.execute(
                f'SELECT name FROM "{workbench_db.schema}".project_versions '
                "WHERE project_id='project-561'"
            )
        ).fetchone()
    assert row == ("synthetic project",)
