"""Real-role startup authority for the Job dispatcher's validation lane (#571).

PostgreSQL only: the role check runs before dependency assembly, so this case
needs no MySQL, MinIO or execution gateway. The probe exists to refuse startup
before any dispatcher (and therefore any claim) exists, so the negative case is
committed rather than trusted to a one-off deployment probe. Deleting the
``voice_validation`` probe in ``_check_role`` makes the first test fail.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr

from speechnexus_backend.job_dispatch_runtime import (
    DispatchDependencies,
    JobDispatchRuntime,
    JobDispatchSettings,
)
from speechnexus_backend.speech.application.job_dispatcher import ResultPublisher
from speechnexus_backend.speech.infrastructure.execution_gateway_adapter import (
    ExecutionGatewayAdapter,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.voice_draft_schema import (
    voice_validation_worker_grants,
)

pytestmark = pytest.mark.integration

DATABASE_VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
ROLE_PASSWORD = "synthetic_dispatch_authority"

# The exact relation and column shapes the startup role check probes.
BASE_DDL = (
    "CREATE TABLE speech_jobs (job_id text)",
    "CREATE TABLE speech_job_events (job_id text, event_seq bigint, state text, "
    "occurred_at_us bigint, kind text, progress_unit text, progress_completed bigint, "
    "progress_total bigint)",
    "CREATE TABLE coordination_jobs (job_id text)",
    "CREATE TABLE admission_operations (operation_id text, request_document text)",
    "CREATE TABLE admission_attempts (attempt_id text)",
    "CREATE TABLE admission_requests (request_document text)",
    "CREATE TABLE admission_leases (lease_id text, lease_state text, cleanup_state text, "
    "deadline_at_us bigint)",
    "CREATE TABLE speech_artifacts (artifact_id text)",
    "CREATE TABLE speech_voice_draft_revisions (draft_id text, revision text)",
    "CREATE TABLE speech_assets (workspace_id text, asset_id text)",
)

# The eight facts the dispatcher needs before the validation lane adds its two.
BASE_TABLES = (
    "speech_jobs",
    "speech_job_events",
    "coordination_jobs",
    "admission_operations",
    "admission_attempts",
    "admission_requests",
    "admission_leases",
    "speech_artifacts",
)


@dataclass
class DispatchSandbox:
    url: str
    schema: str
    role: str
    admin: psycopg.Connection[dict[str, Any]]

    @property
    def role_dsn(self) -> str:
        return make_conninfo(self.url, user=self.role, password=ROLE_PASSWORD)

    def settings(self, kinds: str) -> JobDispatchSettings:
        return JobDispatchSettings(
            process_role="job-worker",
            node_id="node-local-1",
            instance_id="dispatch-authority",
            database_url=SecretStr(self.role_dsn),
            database_schema=self.schema,
            gateway_base_url="http://127.0.0.1:8010",
            gateway_credential=SecretStr("synthetic-component-credential-0001"),
            gateway_timeout_seconds=10,
            batch_size=4,
            lease_ms=30_000,
            poll_seconds=1,
            job_timeout_seconds=60,
            max_attempts=3,
            interval_seconds=5,
            storage_timeout_seconds=3,
            dispatch_kinds=kinds,
            object_storage_endpoint="http://127.0.0.1:59000",
            artifacts_bucket="synthetic-artifacts",
            execution_result_bucket="synthetic-results",
            object_storage_access_key=SecretStr("synthetic-access"),
            object_storage_secret_key=SecretStr("synthetic-secret"),
        )

    def pool(self) -> SpeechPool:
        return speech_pool(self.role_dsn, schema=self.schema, max_size=2, name="dispatch-auth")


@pytest.fixture
def dispatch_db() -> Iterator[DispatchSandbox]:
    """One disposable schema and role with only the eight base facts granted."""
    url = os.environ.get(DATABASE_VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not startup evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_dispatch_" + suffix, "sn_dispatch_role_" + suffix
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        for statement in BASE_DDL:
            admin.execute(statement)
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{ROLE_PASSWORD}'")
        admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        tables = ", ".join(f'"{schema}".{table}' for table in BASE_TABLES)
        admin.execute(f'GRANT SELECT ON {tables} TO "{role}"')
        yield DispatchSandbox(url, schema, role, admin)
    finally:
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


class _Publisher:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Planner:
    async def plan(self, job: Any, submit_intent_id: str) -> Any:
        raise AssertionError("the startup check never plans a Job")


class _Gateway:
    async def query(self, submit_intent_id: str) -> Any:
        raise AssertionError("the startup check never queries the gateway")

    async def aclose(self) -> None:
        return None


def _runtime(db: DispatchSandbox, kinds: str, publisher: _Publisher) -> JobDispatchRuntime:
    return JobDispatchRuntime(
        db.settings(kinds),
        DispatchDependencies(_Planner(), cast(ResultPublisher, publisher), db.pool()),
        gateway=cast(ExecutionGatewayAdapter, _Gateway()),
    )


async def _open_then_close(runtime: JobDispatchRuntime) -> None:
    await runtime.open()
    try:
        assert runtime.dispatcher is not None
    finally:
        await runtime.close()


def test_validation_lane_refuses_a_role_without_the_fact_table_grants(
    dispatch_db: DispatchSandbox,
) -> None:
    publisher = _Publisher()
    runtime = _runtime(dispatch_db, "asr,tts,voice_validation", publisher)

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="speech_voice_draft_revisions"):
        asyncio.run(runtime.open())

    assert runtime.dispatcher is None
    assert publisher.closed == 1


def test_default_lane_does_not_require_the_validation_fact_tables(
    dispatch_db: DispatchSandbox,
) -> None:
    publisher = _Publisher()
    runtime = _runtime(dispatch_db, "asr,tts", publisher)

    asyncio.run(_open_then_close(runtime))

    assert publisher.closed == 1


def test_validation_lane_accepts_the_documented_worker_grant(
    dispatch_db: DispatchSandbox,
) -> None:
    for grant in voice_validation_worker_grants(dispatch_db.schema, dispatch_db.role):
        dispatch_db.admin.execute(grant)
    publisher = _Publisher()
    runtime = _runtime(dispatch_db, "asr,tts,voice_validation", publisher)

    asyncio.run(_open_then_close(runtime))

    assert publisher.closed == 1
