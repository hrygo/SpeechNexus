"""0046 splits the public Job intent out of the admission command body.

Every case starts from 0045, seeds the historical row shape directly, and then
runs the real Alembic upgrade. A skipped database is a failure, not evidence.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
import rfc8785
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from speechnexus_backend.speech.domain.public_jobs import canonical_digest
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
BASE = "0045_pronunciation_set_summary"
HEAD = "0046_public_job_intents"
_INTENT: dict[str, Any] = {
    "input": {"text": "historical intent"},
    "kind": "tts",
    "model": "tts-quality",
}
DOCUMENT_A = rfc8785.dumps(_INTENT).decode("utf-8")
DIGEST_A = canonical_digest(_INTENT)
DIGEST_B = "sha256:" + "b" * 64


@dataclass
class IntentSandbox:
    url: str
    schema: str
    admin: psycopg.Connection[dict[str, Any]]
    config: Config

    def count(self, table: str) -> int:
        cursor = self.admin.execute("SELECT count(*) AS n FROM " + table)
        row = cursor.fetchone()
        assert row is not None
        return int(row["n"])

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = self.admin.execute(statement, parameters or None)
        return list(cursor.fetchall()) if cursor.description else []


def _job(admin: psycopg.Connection[dict[str, Any]], job_id: str, digest: str) -> None:
    admin.execute(
        "INSERT INTO speech_jobs "
        "(job_id, workspace_id, kind, model_id, request_digest, state, status_version, "
        "created_at_us, updated_at_us) "
        "VALUES (%s, 'workspace-default', 'tts', 'tts-quality', %s, 'pending', 1, 1, 1)",
        (job_id, digest),
    )


def _admission(
    admin: psycopg.Connection[dict[str, Any]],
    operation_id: str,
    digest: str,
    document: str | None,
    *,
    recorded_at_us: int = 1,
    redacted: bool = False,
) -> None:
    # A NULL body is only representable as a complete tombstone.
    admin.execute(
        "INSERT INTO admission_operations "
        "(operation_id, workspace_id, request_digest, request_document, "
        "initial_attempt_id, current_attempt_id, recorded_at_us, "
        "command_redacted_at_us, command_deletion_job_id, command_deletion_evidence_digest) "
        "VALUES (%s, 'workspace-default', %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            operation_id,
            digest,
            document,
            operation_id + "-a1",
            operation_id + "-a1",
            recorded_at_us,
            recorded_at_us if redacted else None,
            operation_id + "-delete" if redacted else None,
            DIGEST_B if redacted else None,
        ),
    )


def _seed_history(admin: psycopg.Connection[dict[str, Any]]) -> None:
    """One matching intent plus every reason a legacy row must be skipped."""
    _job(admin, "job-match", DIGEST_A)
    _admission(admin, "job-match", DIGEST_A, DOCUMENT_A, recorded_at_us=7)
    _job(admin, "job-mismatch", DIGEST_A)
    _admission(admin, "job-mismatch", DIGEST_B, '{"kind":"tts"}')
    _job(admin, "job-null", DIGEST_B)
    _admission(admin, "job-null", DIGEST_B, None, recorded_at_us=2, redacted=True)
    _job(admin, "job-none", DIGEST_A)
    # A synchronous operation has no public Job and must not be selected.
    _admission(admin, "operation-sync", DIGEST_A, '{"admit":"sync"}')


@pytest.fixture
def intent_db() -> Iterator[IntentSandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    schema = "sn_intent_mig_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["schema"] = schema
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, BASE)
            admin.execute(f'SET search_path TO "{schema}", pg_catalog')
            _seed_history(admin)
            yield IntentSandbox(url, schema, admin, config)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def test_backfill_only_accepts_matching_legacy_rows_and_reads_back(
    intent_db: IntentSandbox,
) -> None:
    command.upgrade(intent_db.config, HEAD)
    # A repeated upgrade at the same head is a no-op, not a second backfill.
    command.upgrade(intent_db.config, HEAD)
    assert ScriptDirectory.from_config(intent_db.config).get_heads() == [HEAD], (
        "the migration graph must keep a single head"
    )
    assert intent_db.count("speech_job_intents") == 1
    rows = intent_db.query(
        "SELECT job_id, request_digest, request_document, recorded_at_us FROM speech_job_intents"
    )
    assert rows == [
        {
            "job_id": "job-match",
            "request_digest": DIGEST_A,
            "request_document": DOCUMENT_A,
            "recorded_at_us": 7,
        }
    ]

    pool = speech_pool(
        intent_db.url,
        schema=intent_db.schema,
        max_size=1,
        name="intent-migration",
    )
    pool.open()
    try:
        adapter = PostgresPublicJobs(pool)
        intent = adapter.intent_document("job-match")
        assert intent is not None
        assert intent.request_digest == DIGEST_A and intent.document == DOCUMENT_A
        for skipped in ("job-mismatch", "job-null", "job-none"):
            assert adapter.intent_document(skipped) is None
    finally:
        pool.close()

    # Every intent row still has its matching legacy copy, so the in-place
    # rollback only removes the new table.
    command.downgrade(intent_db.config, BASE)
    assert (
        intent_db.query(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = 'speech_job_intents'",
            (intent_db.schema,),
        )
        == []
    )
    command.upgrade(intent_db.config, HEAD)
    assert intent_db.count("speech_job_intents") == 1


def test_downgrade_refuses_when_a_legacy_copy_is_missing(intent_db: IntentSandbox) -> None:
    command.upgrade(intent_db.config, HEAD)
    _job(intent_db.admin, "job-orphan", DIGEST_A)
    intent_db.admin.execute(
        "INSERT INTO speech_job_intents "
        "(job_id, request_digest, request_document, recorded_at_us) "
        "VALUES ('job-orphan', %s, %s, 3)",
        (DIGEST_A, DOCUMENT_A),
    )
    with pytest.raises(DBAPIError, match="no matching legacy copy"):
        command.downgrade(intent_db.config, BASE)
    assert intent_db.count("speech_job_intents") == 2, "a refused downgrade keeps the table"
    intent_db.admin.execute("DELETE FROM speech_job_intents WHERE job_id = 'job-orphan'")
    command.downgrade(intent_db.config, BASE)


def test_downgrade_refuses_when_the_digest_disagrees(intent_db: IntentSandbox) -> None:
    command.upgrade(intent_db.config, HEAD)
    intent_db.admin.execute(
        "UPDATE speech_job_intents SET request_digest = %s WHERE job_id = 'job-match'",
        (DIGEST_B,),
    )
    with pytest.raises(DBAPIError, match="no matching legacy copy"):
        command.downgrade(intent_db.config, BASE)
    assert intent_db.count("speech_job_intents") == 1, "a refused downgrade keeps the table"
    intent_db.admin.execute(
        "UPDATE speech_job_intents SET request_digest = %s WHERE job_id = 'job-match'",
        (DIGEST_A,),
    )
    command.downgrade(intent_db.config, BASE)
    assert (
        intent_db.query(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = 'speech_job_intents'",
            (intent_db.schema,),
        )
        == []
    )
