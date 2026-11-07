"""Public Job submit/read on an isolated PostgreSQL schema; no skip counts as evidence."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.public_jobs import (
    PublicJobConflictError,
    canonical_digest,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_reconciliation_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.publications_sql import append_publication
from speechnexus_backend.speech.infrastructure.voice_draft_schema import voice_draft_grants

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_public_job_test"
DIGEST = "sha256:" + "a" * 64
PREFIX = "/v1/nexus/jobs"
RETRY = PREFIX + "/{job_id}/retries"


@dataclass
class JobSandbox:
    url: str = field(repr=False)
    schema: str
    role: str
    admin: psycopg.Connection[dict[str, Any]] = field(repr=False)
    jobs: PublicJobs = field(repr=False)
    pool: Any = field(repr=False)

    def count(self, table: str) -> int:
        with self.admin.cursor() as cursor:
            cursor.execute("SELECT count(*) AS n FROM " + table)
            row = cursor.fetchone()
            assert row is not None
            return int(row["n"])

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)
            return list(cursor.fetchall()) if cursor.description else []


def _seed_binding(admin: psycopg.Connection[dict[str, Any]]) -> None:
    admin.execute(
        "INSERT INTO speech_binding_families (binding_id, latest_revision) "
        "VALUES ('tts-quality', '1')"
    )
    admin.execute(
        "INSERT INTO speech_binding_commands "
        "(key_digest, binding_id, document_version, intent_json, request_digest) "
        "VALUES (%s, 'tts-quality', 1, '{}', %s)",
        (DIGEST, DIGEST),
    )
    definition = {"binding_id": "tts-quality", "tasks": ["tts_file"], "release_tier": "production"}
    admin.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id, revision, revision_id, key_digest, document_version, definition_json, "
        "definition_digest, request_digest, created_at_us) "
        "VALUES ('tts-quality', '1', 'binding-rev-1', %s, 1, %s, %s, %s, 1)",
        (DIGEST, json.dumps(definition), DIGEST, DIGEST),
    )
    with admin.transaction(), admin.cursor() as cursor:
        assert append_publication(
            cursor,
            Publication("binding", "tts-quality", 1, "binding-rev-1", 1),
            expected_current_revision=None,
            expected_catalog_revision=1,
        )
        assert append_publication(cursor, Publication("voice", "voice-vivian", 2, "voice-rev-2", 2))


@pytest.fixture
def jobs_db() -> Iterator[JobSandbox]:
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
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        for grant in (
            *job_runtime_grants(schema, role),
            *public_job_grants(schema, role),
            *voice_draft_grants(schema, role),
            # Creating a public Job now also commits the coordination claim and the
            # private original command in the same transaction, so the writer role
            # needs the production Job-writer privileges as well.
            *job_runtime_grants(schema, role),
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
            make_conninfo(url, user=role, password=PASSWORD),
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


def _settings_for(db: JobSandbox) -> Any:
    return _settings().model_copy(
        update={
            "database_schema": db.schema,
            "job_writer_database_url": SecretStr(
                make_conninfo(db.url, user=db.role, password=PASSWORD)
            ),
        }
    )


def _app(db: JobSandbox) -> Any:
    return create_app(settings=_settings_for(db), jobs=db.jobs)


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic public job"},
        "voice": {"id": "voice-vivian", "revision": "2"},
        "client_reference": "synthetic-reference",
    }
    body.update(changes)
    return body


async def test_submit_read_replay_and_conflict_against_real_pg(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    headers = {"Idempotency-Key": "synthetic-idem-0001"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(PREFIX, json=_body(), headers=headers)
        assert created.status_code == 202, created.text
        first = created.json()
        assert first["object"] == "nexus.job" and first["status"] == "queued"
        assert first["effective_configuration"]["models"]["tts"] == {
            "id": "tts-quality",
            "revision": "1",
        }
        assert first["effective_configuration"]["voice"] == {
            "id": "voice-vivian",
            "revision": "2",
        }
        assert created.headers["Location"] == PREFIX + "/" + first["id"]
        assert jobs_db.count("speech_jobs") == 1
        assert jobs_db.count("speech_job_events") == 1
        assert jobs_db.count("speech_job_submissions") == 1
        assert jobs_db.count("admission_operations") == 1
        assert jobs_db.count("speech_job_intents") == 1
        assert jobs_db.count("coordination_jobs") == 1
        assert jobs_db.count("coordination_outbox") == 1
        ledger = jobs_db.query("SELECT request_document, request_digest FROM admission_operations")[
            0
        ]
        intent = jobs_db.query(
            "SELECT request_digest, request_document FROM speech_job_intents WHERE job_id = %s",
            (first["id"],),
        )[0]
        # The authoritative intent and the legacy dual-write copy agree.
        assert intent["request_digest"] == ledger["request_digest"]
        assert intent["request_document"] == ledger["request_document"]
        assert (
            jobs_db.query(
                "SELECT request_digest FROM speech_jobs WHERE job_id = %s", (first["id"],)
            )[0]["request_digest"]
            == ledger["request_digest"]
        )
        assert "synthetic public job" in ledger["request_document"]

        read = await client.get(PREFIX + "/" + first["id"])
        assert read.status_code == 200, read.text
        assert read.json() == first

        replay = await client.post(PREFIX, json=_body(), headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["id"] == first["id"]
        assert jobs_db.count("speech_jobs") == 1
        assert jobs_db.count("speech_job_events") == 1
        assert jobs_db.count("speech_job_submissions") == 1
        assert jobs_db.count("admission_operations") == 1
        assert jobs_db.count("speech_job_intents") == 1
        assert jobs_db.count("coordination_jobs") == 1
        assert jobs_db.count("coordination_outbox") == 1

        conflict = await client.post(
            PREFIX, json=_body(client_reference="other-intent"), headers=headers
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        assert jobs_db.count("speech_jobs") == 1

        missing = await client.get(PREFIX + "/job-00000000000000000000000000000000")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "job_not_found"
        invalid = await client.get(PREFIX + "/NOT_VALID")
        assert invalid.status_code == 404


def _fail_job(db: JobSandbox, job_id: str) -> None:
    db.jobs.advance(job_id, "running")
    db.jobs.advance(job_id, "failed", error_code="engine_failure")


async def test_retry_reads_the_authoritative_intent_not_the_legacy_copy(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(PREFIX, json=_body(), headers={"Idempotency-Key": "intent-a"})
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        _fail_job(jobs_db, job_id)
        # A2 will rewrite the legacy column; the public intent must not be read
        # back from it. The digest is left intact so only the read source differs.
        jobs_db.admin.execute(
            "UPDATE admission_operations SET request_document = %s WHERE operation_id = %s",
            ('{"kind":"tts","model":"tts-quality","input":{"text":"legacy rewrite"}}', job_id),
        )
        retried = await client.post(
            RETRY.format(job_id=job_id), headers={"Idempotency-Key": "intent-a-child"}
        )
        assert retried.status_code == 202, retried.text
        child = retried.json()
        intent = jobs_db.query(
            "SELECT request_document FROM speech_job_intents WHERE job_id = %s", (child["id"],)
        )[0]
        document = json.loads(intent["request_document"])
        assert document["input"] == {"text": "synthetic public job"}
        assert jobs_db.count("speech_jobs") == 2


async def test_tampered_intent_document_is_rejected_instead_of_retried(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(PREFIX, json=_body())
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        _fail_job(jobs_db, job_id)
        document = json.loads(
            jobs_db.query(
                "SELECT request_document FROM speech_job_intents WHERE job_id = %s", (job_id,)
            )[0]["request_document"]
        )
        # A valid but different business document: only the digest separates it
        # from the accepted intent, so the unchanged check must refuse it.
        document["client_reference"] = "tampered-intent"
        jobs_db.admin.execute(
            "UPDATE speech_job_intents SET request_document = %s WHERE job_id = %s",
            (json.dumps(document, sort_keys=True, separators=(",", ":")), job_id),
        )
        retried = await client.post(
            RETRY.format(job_id=job_id), headers={"Idempotency-Key": "tampered-child"}
        )
        assert retried.status_code == 503, retried.text
        assert retried.json()["error"]["code"] == "job_unavailable"
        assert jobs_db.count("speech_jobs") == 1


async def test_forged_self_consistent_intent_is_refused(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(PREFIX, json=_body())
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        _fail_job(jobs_db, job_id)
        document = json.loads(
            jobs_db.query(
                "SELECT request_document FROM speech_job_intents WHERE job_id = %s", (job_id,)
            )[0]["request_document"]
        )
        # A self-consistent replacement (document and digest agree) would pass
        # the domain check but still disagrees with the Job identity. The read
        # path must cross-check the two digests, not just internal consistency.
        document["client_reference"] = "forged-intent"
        forged_document = json.dumps(document, sort_keys=True, separators=(",", ":"))
        jobs_db.admin.execute(
            "UPDATE speech_job_intents SET request_digest = %s, request_document = %s "
            "WHERE job_id = %s",
            (canonical_digest(document), forged_document, job_id),
        )
        retried = await client.post(
            RETRY.format(job_id=job_id), headers={"Idempotency-Key": "forged-child"}
        )
        assert retried.status_code == 503, retried.text
        assert retried.json()["error"]["code"] == "job_unavailable"
        assert jobs_db.count("speech_jobs") == 1


async def test_missing_intent_row_never_falls_back_to_the_legacy_copy(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(PREFIX, json=_body())
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        _fail_job(jobs_db, job_id)
        # A missing backfill or a deleted intent leaves no replayable document.
        # The legacy copy stays intact and must not become a fallback.
        jobs_db.admin.execute("DELETE FROM speech_job_intents WHERE job_id = %s", (job_id,))
        legacy = jobs_db.query(
            "SELECT request_document FROM admission_operations WHERE operation_id = %s",
            (job_id,),
        )[0]
        assert legacy["request_document"] is not None
        read = await client.get(PREFIX + "/" + job_id)
        assert read.status_code == 200, read.text
        retried = await client.post(
            RETRY.format(job_id=job_id), headers={"Idempotency-Key": "missing-child"}
        )
        assert retried.status_code == 409, retried.text
        assert retried.json()["error"]["code"] == "job_retry_unavailable"
        assert jobs_db.count("speech_jobs") == 1


async def test_success_and_failure_invariants_are_enforced_twice(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(PREFIX, json=_body())
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]

    jobs_db.jobs.advance(job_id, "running")
    with pytest.raises(PublicJobConflictError):
        jobs_db.jobs.advance(job_id, "succeeded")
    jobs_db.jobs.advance(
        job_id,
        "succeeded",
        result_json='{"kind":"tts","audio_artifact_id":"artifact-synthetic","receipt_id":"receipt-synthetic"}',
    )
    row = jobs_db.query("SELECT state, status_version, result_json FROM speech_jobs")[0]
    assert row["state"] == "succeeded" and row["status_version"] == 3
    assert row["result_json"] is not None

    with pytest.raises(psycopg.errors.CheckViolation):
        jobs_db.admin.execute(
            "UPDATE speech_jobs SET result_json = NULL, status_version = status_version + 1 "
            "WHERE job_id = %s",
            (job_id,),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        jobs_db.admin.execute(
            "UPDATE speech_jobs SET error_code = 'synthetic_error', "
            "status_version = status_version + 1 WHERE job_id = %s",
            (job_id,),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        jobs_db.admin.execute(
            "UPDATE speech_jobs SET status_version = 9 WHERE job_id = %s", (job_id,)
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        jobs_db.admin.execute(
            "UPDATE speech_jobs SET state = 'running', status_version = status_version + 1 "
            "WHERE job_id = %s",
            (job_id,),
        )


async def test_composition_root_opens_verified_writer_and_serves_the_job(
    jobs_db: JobSandbox,
) -> None:
    app = create_app(settings=_settings_for(jobs_db))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        created = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-runtime-01"}
        )
        assert created.status_code == 202, created.text
        read = await client.get(PREFIX + "/" + created.json()["id"])
        assert read.status_code == 200, read.text
        assert read.json() == created.json()


def test_voice_draft_revision_grants_are_append_only(jobs_db: JobSandbox) -> None:
    role = jobs_db.role
    assert role
    revision_table = f"{jobs_db.schema}.speech_voice_draft_revisions"
    draft_table = f"{jobs_db.schema}.speech_voice_drafts"
    row = jobs_db.query(
        "SELECT pg_catalog.has_table_privilege(%s, %s, 'SELECT') AS can_select, "
        "pg_catalog.has_table_privilege(%s, %s, 'INSERT') AS can_insert, "
        "pg_catalog.has_table_privilege(%s, %s, 'UPDATE') AS can_update, "
        "pg_catalog.has_table_privilege(%s, %s, 'DELETE') AS can_delete, "
        "pg_catalog.has_column_privilege(%s, %s, 'head_revision', 'UPDATE') AS can_head",
        (
            role,
            revision_table,
            role,
            revision_table,
            role,
            revision_table,
            role,
            revision_table,
            role,
            draft_table,
        ),
    )[0]
    assert row["can_select"] is True
    assert row["can_insert"] is True
    assert row["can_update"] is False
    assert row["can_delete"] is False
    assert row["can_head"] is True
    try:
        jobs_db.admin.execute(f'SET ROLE "{role}"')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            jobs_db.admin.execute(f"UPDATE {revision_table} SET revision='2' WHERE false")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            jobs_db.admin.execute(f"DELETE FROM {revision_table} WHERE false")
    finally:
        jobs_db.admin.execute("RESET ROLE")
