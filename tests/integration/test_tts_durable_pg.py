"""PostgreSQL-only TTS persistence and voice retirement serialization.

This separate fixture is not the dual-database coordination acceptance gate.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_transcription_disable_barrier_pg import _seed
from tests.unit.backend.test_tts_durable_intent import plan

from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.speech_synthesis import SpeechUnavailableError
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
    tts_voice_admission_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.jobs_postgres import (
    PostgresJobTransactions,
    job_pool,
)
from speechnexus_backend.speech.infrastructure.tts_admission import (
    PostgresOriginalTtsCommands,
    PostgresTtsAdmission,
    PostgresTtsFacts,
    TtsRecipe,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def database() -> Iterator[JobDatabase]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Isolated PostgreSQL URL required")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_tts_" + suffix, "sn_tts_role_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    pool = None
    role_created = False
    try:
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_job_test'")
        role_created = True
        for grant in job_runtime_grants(schema, role) + job_dispatch_grants(schema, role):
            admin.execute(grant)
        pool = job_pool(make_conninfo(url, user=role, password="synthetic_job_test"), schema=schema)
        pool.open()
        yield JobDatabase(
            "postgresql", url, schema, role, admin, PostgresJobTransactions(pool), pool
        )
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        if role_created:
            admin.execute(f'DROP ROLE "{role}"')
        admin.close()


def adapter(admission: AdmissionDatabase, *, seed: bool = True) -> PostgresTtsAdmission:
    db = admission.runtime.foundation.database
    if seed:
        _seed(admission)
        db.query(
            "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
            "VALUES ('voice','voice-test','2','voice-revision-test',1)"
        )
        for grant in tts_voice_admission_grants(
            db.schema, admission.runtime.foundation.speech_role
        ):
            db.query(grant)
    pool = admission.runtime.pool
    assert pool is not None
    # The transaction/retirement test does not claim this synthetic callback is qualification.
    port = PostgresAdmissionTransactions(
        pool,
        max_command_bytes=1_048_576,
        timeout_seconds=10,
        require_builtin_voice=lambda *a, **k: None,
    )
    recipe = TtsRecipe(
        "model-test",
        "stage-main",
        "instance-new",
        1,
        replace(
            admission.command.approved_recipe,
            admission_expires_at_us=admission.runtime.now() + 60_000_000,
        ),
    )
    return PostgresTtsAdmission(
        PostgresTtsFacts(pool), AdmissionService(port), PostgresOriginalTtsCommands(pool), recipe
    )


def test_postgres_persists_and_recovers_original(admission_database: AdmissionDatabase) -> None:
    service = adapter(admission_database)
    request = service.admit(plan())
    row = admission_database.runtime.foundation.database.query(
        "SELECT request_document FROM admission_operations WHERE operation_id=%s",
        (request["operation_id"],),
    )[0]
    assert "Synthetic text." in row["request_document"]
    restarted = adapter(admission_database, seed=False)
    restored_plan, restored_request = restarted.recover("request-test")
    assert restored_plan == plan()
    assert restored_request == request
    db = admission_database.runtime.foundation.database
    db.query(
        "INSERT INTO speech_publication_retirements "
        "(kind,object_id,revision,revision_id,retired_at_us) "
        "VALUES ('voice','voice-test','2','voice-revision-test',2)"
    )
    assert restarted.admit(plan()) == request
    with pytest.raises(SpeechUnavailableError):
        restarted.admit(replace(plan(), request_id="request-new"))
    assert db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"] == 1


def test_retirement_waits_and_new_admission_rechecks(admission_database: AdmissionDatabase) -> None:
    service = adapter(admission_database)
    db = admission_database.runtime.foundation.database
    blocker = db.blocker()
    try:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':voice:' || %s, 0))",
            ("voice-test",),
        )
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(service.admit, plan())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if db.query(
                    "SELECT pid FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                    (blocker.info.backend_pid,),
                ):
                    break
                if pending.done():
                    pending.result()
                    pytest.fail("admission completed before the voice lock was released")
                time.sleep(0.01)
            else:
                pytest.fail("no admission transaction waited on the voice lock")
            blocker.execute(
                "INSERT INTO speech_publication_retirements "
                "(kind,object_id,revision,revision_id,retired_at_us) "
                "VALUES ('voice','voice-test','2','voice-revision-test',2)"
            )
            blocker.commit()
            with pytest.raises(SpeechUnavailableError):
                pending.result(timeout=10)
        assert db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"] == 0
    finally:
        blocker.close()
