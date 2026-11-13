"""Job writer companion identities must target the writer's own database.

Issue #660, second half: when the voice publisher or admission pool pointed at a
different database, ``JobRuntime.open()`` rejected startup with a generic
``Public Job writer startup rejected; verify migration and writer role`` that did
not name the publisher or the same-database rule. The message is now specific, and
it is the only startup evidence that this operator mistake is reported at all.

Real PostgreSQL only: the refusal compares the live database identity, so a
synthetic DSN cannot prove it. ``tests/unit/backend/test_startup_key_pairs.py``
proves the composition-root half (a companion DSN without the writer key).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.unit.backend.test_health import _settings

from speechnexus_backend.job_runtime import JobRuntime
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_reconciliation_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_admission_grants import (
    public_job_admission_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_backend.speech.infrastructure.voice_draft_schema import voice_draft_grants
from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    voice_publication_grants,
)

pytestmark = pytest.mark.integration

VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_job_pair_test"
ROOT = Path(__file__).resolve().parents[2]

JOB_WRITER = "SPEECHNEXUS_JOB_WRITER_DATABASE_URL"
VOICE_PUBLISHER = "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL"
JOB_ADMISSION = "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL"


@dataclass
class PairSandbox:
    url: str = ""
    schema: str = ""
    writer_role: str = ""
    publisher_role: str = ""
    admission_role: str = ""
    second_url: str = ""
    second_database: str = ""
    admin: psycopg.Connection[dict[str, Any]] | None = None


def _database_url(url: str, name: str) -> str:
    """URL form of ``url`` with ``name`` as the database, for SQLAlchemy."""
    base, _, _ = url.rpartition("/")
    return f"{base}/{name}"


def _migrate(url: str, schema: str) -> None:
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


def _writer_grants(schema: str, role: str) -> tuple[str, ...]:
    return (
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
    )


@pytest.fixture
def pair_db() -> Iterator[PairSandbox]:
    """One migrated schema plus a second, independently migrated database.

    Both databases share the schema name because they are separate databases; the
    startup check compares ``current_database()``, host and port, not the schema.
    """
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not configuration evidence")
    suffix = uuid.uuid4().hex[:10]
    schema = "sn_pair_" + suffix
    writer_role = "sn_pair_writer_" + suffix
    publisher_role = "sn_pair_pub_" + suffix
    admission_role = "sn_pair_adm_" + suffix
    second_database = "sn_pair_db_" + suffix
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    second_admin: psycopg.Connection[dict[str, Any]] | None = None
    created = False
    try:
        _migrate(url, schema)
        admin.execute(f'CREATE DATABASE "{second_database}"')
        created = True
        second_url = make_conninfo(url, dbname=second_database)
        _migrate(_database_url(url, second_database), schema)
        for role in (writer_role, publisher_role, admission_role):
            admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        for grant in _writer_grants(schema, writer_role):
            admin.execute(grant)
        # The same publisher role is granted in both databases: same-database
        # success and cross-database refusal then differ only in the DSN.
        for grant in voice_publication_grants(schema, publisher_role):
            admin.execute(grant)
        second_admin = psycopg.connect(second_url, autocommit=True, row_factory=dict_row)
        second_admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        for grant in (
            *voice_publication_grants(schema, publisher_role),
            *public_job_admission_grants(schema, admission_role),
        ):
            second_admin.execute(grant)
        yield PairSandbox(
            url=url,
            schema=schema,
            writer_role=writer_role,
            publisher_role=publisher_role,
            admission_role=admission_role,
            second_url=second_url,
            second_database=second_database,
            admin=admin,
        )
    finally:
        if second_admin is not None:
            for role in (writer_role, publisher_role, admission_role):
                with contextlib.suppress(psycopg.errors.UndefinedObject):
                    second_admin.execute(f'DROP OWNED BY "{role}"')
            second_admin.close()
        for role in (writer_role, publisher_role, admission_role):
            with contextlib.suppress(psycopg.errors.UndefinedObject):
                admin.execute(f'DROP OWNED BY "{role}"')
                admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        if created:
            admin.execute(f'DROP DATABASE IF EXISTS "{second_database}" WITH (FORCE)')
        admin.close()


def _runtime_settings(db: PairSandbox, **changes: Any) -> Any:
    return _settings().model_copy(
        update={
            "database_schema": db.schema,
            "job_writer_database_url": SecretStr(
                make_conninfo(db.url, user=db.writer_role, password=PASSWORD)
            ),
        }
        | changes
    )


def test_a_publisher_on_the_same_database_opens(pair_db: PairSandbox) -> None:
    """The baseline for the refusal: the same two DSNs, one database, work."""
    db = pair_db
    runtime = JobRuntime(
        _runtime_settings(
            db,
            voice_publisher_database_url=SecretStr(
                make_conninfo(db.url, user=db.publisher_role, password=PASSWORD)
            ),
        )
    )
    try:
        runtime.open()
        assert runtime.voice_publications is not None
    finally:
        runtime.close()


def test_a_publisher_on_another_database_is_refused_naming_both_keys(
    pair_db: PairSandbox,
) -> None:
    db = pair_db
    runtime = JobRuntime(
        _runtime_settings(
            db,
            voice_publisher_database_url=SecretStr(
                make_conninfo(db.second_url, user=db.publisher_role, password=PASSWORD)
            ),
        )
    )

    with pytest.raises(ValueError) as caught:
        runtime.open()

    message = str(caught.value)
    assert VOICE_PUBLISHER in message
    assert JOB_WRITER in message
    assert "must connect to the same database" in message
    assert "unset SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL" in message
    # The shared cleanup still ran: no half-open pool survives the refusal.
    assert runtime.jobs is None
    assert runtime._pool is None
    assert runtime._publisher_pool is None


def test_an_admission_on_another_database_is_refused_naming_both_keys(
    pair_db: PairSandbox,
) -> None:
    db = pair_db
    runtime = JobRuntime(
        _runtime_settings(
            db,
            job_admission_database_url=SecretStr(
                make_conninfo(db.second_url, user=db.admission_role, password=PASSWORD)
            ),
        )
    )

    with pytest.raises(ValueError) as caught:
        runtime.open()

    message = str(caught.value)
    assert JOB_ADMISSION in message
    assert JOB_WRITER in message
    assert "must connect to the same database" in message
    assert "unset SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL" in message
    assert runtime._pool is None
    assert runtime._admission_pool is None
