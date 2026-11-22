"""The three missing-prerequisite shapes against isolated PostgreSQL; no skip counts.

#920 requires the failure surface to name the condition on a real database rather
than on a fake:

* an empty schema (the migration profile never ran) answers "run the migration
  profile" on the create, the list and the writer runtime's own startup, without a
  driver traceback and without being reported as a role-permission fault;
* a deployment with both Job DSNs blank answers with the writer key only;
* a deployment with the writer DSN set and the admission DSN blank answers with the
  admission key only.

The last two must differ, and neither may echo a DSN value, host, database name or
credential. The isolation rule is the repository's: one throwaway schema and role per
test, dropped afterwards, or the test fails instead of skipping.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from tests.integration.test_public_jobs_http import (
    PASSWORD,
    JobSandbox,
    _body,
)
from tests.integration.test_public_jobs_http import (
    jobs_db as jobs_db,
)
from tests.support.portable_pg import connect as portable_pg_connect
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.job_runtime import JobRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.job_prerequisites import (
    ADMISSION_DSN,
    ADMISSION_DSN_KEY,
    PROVISION_ROLES_COMMAND,
    ROLE_PRIVILEGES_MISSING,
    SCHEMA_NOT_MIGRATED,
    WRITER_DSN,
    WRITER_DSN_KEY,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.infrastructure.job_schema_preflight import (
    JobSchemaNotMigratedError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.interfaces.http.job_errors import job_storage_error

pytestmark = pytest.mark.integration
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PREFIX = "/v1/nexus/jobs"


@pytest.fixture
def empty_database() -> Iterator[tuple[str, str]]:
    """One schema with no migration applied and no grants, dropped afterwards."""
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not Job evidence")
    schema = "sn_empty_" + uuid.uuid4().hex[:12]
    admin = portable_pg_connect(url, autocommit=True, row_factory=dict_row)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        yield url, schema
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _sandbox_settings(sandbox: JobSandbox) -> Settings:
    """The writer identity of the sandbox, with no admission DSN: that is the shape."""
    return _settings().model_copy(
        update={
            "database_schema": sandbox.schema,
            "job_writer_database_url": SecretStr(
                make_conninfo(sandbox.url, user=sandbox.role, password=PASSWORD)
            ),
        }
    )


def _message(response: Any) -> str:
    # An httpx response and a starlette JSONResponse both carry the same envelope.
    body = response.text if hasattr(response, "text") else bytes(response.body).decode()
    return str(json.loads(body)["error"]["message"])


async def test_an_unmigrated_database_answers_the_migration_before_the_role_guard(
    empty_database: tuple[str, str],
) -> None:
    """The startup refusal names the migration, not a missing grant.

    The writer guard reads ``pg_catalog`` for the relations it may touch; on an empty
    schema it would answer "lacks SELECT on ...", which sends an operator to re-grant
    a role that has nothing to grant privileges on. The migration check therefore runs
    first and raises its own named refusal.
    """
    url, schema = empty_database
    settings = _settings().model_copy(
        update={
            "database_schema": schema,
            "job_writer_database_url": SecretStr(url),
        }
    )
    runtime = JobRuntime(settings)
    with pytest.raises(JobSchemaNotMigratedError) as raised:
        runtime.open()

    message = str(raised.value)
    assert message == SCHEMA_NOT_MIGRATED.detail
    assert "migration profile" in message
    assert "lacks SELECT" not in message
    # The driver's own sentence never reaches the operator.
    assert "does not exist" not in message
    assert "alembic_version" in raised.value.missing
    assert runtime.jobs is None


async def test_a_create_and_a_list_on_an_unmigrated_database_name_the_migration(
    empty_database: tuple[str, str],
) -> None:
    """Both the write path and the read path answer the same prerequisite, as JSON."""
    url, schema = empty_database
    pool = speech_pool(url, schema=schema, max_size=2, name="sn-unmigrated-" + uuid.uuid4().hex[:8])
    pool.open()
    try:
        adapter = PostgresPublicJobs(pool, timeout_seconds=3)
        app = create_app(
            settings=_settings().model_copy(update={"database_schema": schema}),
            jobs=PublicJobs(adapter, adapter),
        )
        async with _client(app) as client:
            created = await client.post(PREFIX, json=_body())
            listed = await client.get(PREFIX)
            # The single-Job read used to escape this shape as an unhandled 500.
            read = await client.get(PREFIX + "/job-00000000000000000000000000000001")
    finally:
        pool.close()

    for response in (created, listed, read):
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["code"] == "job_unavailable"
        assert error["retryable"] is False
        assert error["message"] == SCHEMA_NOT_MIGRATED.detail
        # No traceback can hide in a JSON error envelope, and the driver's relation
        # name is not what the operator acts on.
        assert "Traceback" not in response.text
        assert "does not exist" not in response.text


async def test_a_blank_writer_dsn_and_a_blank_admission_dsn_answer_differently(
    jobs_db: JobSandbox,
) -> None:
    """The two configuration misses are distinguishable from the response alone.

    The first deployment never assembled the Job surface (writer blank, so a reader
    may not blame the admission key the create path never reaches). The second
    assembled it with the writer and left the admission key blank: reads work and a
    create is the one operation that refuses. Their messages must differ, and each
    names only its own key.
    """
    unassembled = create_app(settings=_settings())
    async with _client(unassembled) as client:
        writer_response = await client.post(PREFIX, json=_body())

    runtime = JobRuntime(_sandbox_settings(jobs_db))
    runtime.open()
    try:
        assembled = create_app(
            settings=_settings().model_copy(update={"database_schema": jobs_db.schema}),
            jobs=runtime.jobs,
        )
        async with _client(assembled) as client:
            admission_response = await client.post(PREFIX, json=_body())
    finally:
        runtime.close()

    assert writer_response.status_code == 503, writer_response.text
    assert admission_response.status_code == 503, admission_response.text
    writer_message = _message(writer_response)
    admission_message = _message(admission_response)

    assert writer_message == WRITER_DSN.detail
    assert admission_message == ADMISSION_DSN.detail
    assert writer_message != admission_message
    # Each answer names one key: the other absence is not blamed here.
    assert WRITER_DSN_KEY in writer_message and ADMISSION_DSN_KEY not in writer_message
    assert ADMISSION_DSN_KEY in admission_message and WRITER_DSN_KEY not in admission_message

    # Neither answer echoes a deployment's DSN, host, database or credential.
    for response in (writer_response, admission_response):
        body = response.text
        for secret in (jobs_db.url, jobs_db.role, jobs_db.schema, "postgresql://"):
            assert secret not in body, (secret, body)


def test_the_runtime_hands_the_shared_admission_row_to_the_store() -> None:
    """The runtime embeds the table's row, not a second copy of the sentence."""
    from speechnexus_backend import job_runtime

    assert ADMISSION_DSN.detail == job_runtime._ADMISSION_DSN_BLANK


async def test_a_blank_admission_dsn_keeps_the_reader_surface(
    jobs_db: JobSandbox,
) -> None:
    """Reads and cancellations keep working without the admission role.

    This is the asymmetry the operator map states: the admission key refuses creates
    only, so the answer must not claim the whole surface is down.
    """
    runtime = JobRuntime(_sandbox_settings(jobs_db))
    runtime.open()
    try:
        app = create_app(
            settings=_settings().model_copy(update={"database_schema": jobs_db.schema}),
            jobs=runtime.jobs,
        )
        async with _client(app) as client:
            listed = await client.get(PREFIX)
    finally:
        runtime.close()
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == []


def test_the_store_classifies_a_real_missing_relation_as_the_migration(
    jobs_db: JobSandbox,
) -> None:
    """The driver's own ``UndefinedTable`` becomes the migration row, not a bare 503.

    ``speech_jobs`` is dropped from a fully migrated sandbox, so the store answers a
    real psycopg failure: the refusal carries the shared migration text and the
    driver's sentence never reaches the boundary.
    """
    jobs_db.query("DROP TABLE speech_jobs CASCADE")

    with pytest.raises(PublicJobUnavailableError) as raised:
        jobs_db.jobs.list_jobs(workspace_id="workspace-default")

    assert raised.value.detail == SCHEMA_NOT_MIGRATED.detail
    assert "does not exist" not in str(raised.value)

    response = job_storage_error("req-real-driver", raised.value)
    assert response.status_code == 503
    assert _message(response) == SCHEMA_NOT_MIGRATED.detail


async def test_a_revoked_grant_names_the_provisioning_script_not_the_migration(
    jobs_db: JobSandbox,
) -> None:
    """SQLSTATE ``42501`` on a migrated database is a grant repair, not a migration.

    Both conditions refuse the same read, so this is the pair that used to be
    indistinguishable: the schema is at head and only the role's privilege is gone.
    """
    jobs_db.query(f'REVOKE SELECT ON "{jobs_db.schema}".speech_jobs FROM "{jobs_db.role}"')

    app = create_app(
        settings=_settings().model_copy(update={"database_schema": jobs_db.schema}),
        jobs=jobs_db.jobs,
    )
    async with _client(app) as client:
        response = await client.get(PREFIX)

    assert response.status_code == 503, response.text
    message = _message(response)
    assert message == ROLE_PRIVILEGES_MISSING.detail
    assert PROVISION_ROLES_COMMAND in message
    # The migration answer would send an operator to re-run a migration that is
    # already at head, so the two rows must not be interchangeable.
    assert "migration profile" not in message
    assert message != SCHEMA_NOT_MIGRATED.detail
