"""Real PostgreSQL claim proof for the public-Job dispatcher; no skip counts as evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchClaimLostError,
    DispatchJob,
    DispatchLimits,
    ExecutionSubmission,
    PublishedResult,
)
from speechnexus_backend.speech.infrastructure.execution_gateway_adapter import (
    ExecutionGatewayAdapter,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
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

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_dispatch_test"
DIGEST = "sha256:" + "a" * 64
SUBMIT_ID = "submit-" + "b" * 32


@dataclass
class DispatchSandbox:
    url: str = field(repr=False)
    schema: str
    role: str
    admin: psycopg.Connection[dict[str, Any]] = field(repr=False)
    jobs: PublicJobs = field(repr=False)
    pool: Any = field(repr=False)

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
    admin.execute(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('binding', 'tts-quality', '1', 'binding-rev-1', 1), "
        "('voice', 'voice-vivian', '2', 'voice-rev-2', 2)"
    )


@pytest.fixture
def dispatch_db() -> Iterator[DispatchSandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not dispatcher evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_dispatch_" + suffix, "sn_dispatch_role_" + suffix
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
            *job_dispatch_grants(schema, role),
            *public_job_grants(schema, role),
            *public_job_submission_grants(schema, role),
            f'GRANT SELECT ON "{schema}".admission_operations TO "{role}"',
            f"GRANT UPDATE (claim_owner_id, claim_expires_at_us, claim_version, job_state) "
            f'ON "{schema}".coordination_jobs TO "{role}"',
            f"GRANT SELECT ON speech_workspaces, speech_publications, "
            f"speech_catalog_state, speech_publication_retirements, "
            f"speech_binding_revisions, speech_assets, speech_pronunciation_revisions "
            f'TO "{role}"',
        ):
            admin.execute(grant)
        _seed_binding(admin)
        pool = speech_pool(
            make_conninfo(url, user=role, password=PASSWORD),
            schema=schema,
            max_size=4,
            name="dispatch-" + suffix,
        )
        pool.open()
        adapter = PostgresPublicJobs(pool, timeout_seconds=3)
        yield DispatchSandbox(url, schema, role, admin, PublicJobs(adapter, adapter), pool)
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


class Planner:
    async def plan(self, job: DispatchJob, submit_id: str) -> ExecutionSubmission:
        return ExecutionSubmission(
            submit_id,
            {
                "submit_intent_id": submit_id,
                "lease_id": "lease-synthetic",
                "operation_id": job.job_id,
                "attempt_id": job.job_id + "-a1",
                "stage_id": "stage-synthetic",
                "workspace_id": job.workspace_id,
                "pool_id": "tts-batch",
                "host_node_id": "node-synthetic",
                "gateway_id": "gateway-synthetic",
                "instance_id": "instance-synthetic",
                "instance_generation": 1,
                "binding_revision_id": "binding-rev-1",
                "policy_revision_id": "policy-rev-1",
                "voice_revision_id": None,
                "fencing_token": 1,
                "command_digest": DIGEST,
                "command": {"schema_version": "1.0.0", "kind": "tts"},
                "deadline_at": "2026-10-27T00:00:00Z",
                "max_output_audio_ms": 1000,
                "sample_rate_hz": 24000,
            },
        )


class Publisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(
        self, job: DispatchJob, submission: ExecutionSubmission, receipt: Mapping[str, Any]
    ) -> PublishedResult:
        self.calls += 1
        return PublishedResult("artifact-synthetic", "receipt-synthetic")


class Gateway:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.submit_id = SUBMIT_ID

    def status(self, state: str) -> dict[str, Any]:
        return {"submit_intent_id": self.submit_id, "execution_state": state}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/internal/v1/executions/submit":
            self.submit_calls += 1
            self.submit_id = json.loads(request.content)["submit_intent_id"]
            return httpx.Response(200, json=self.status("executing"))
        if request.method == "GET" and path.startswith("/internal/v1/executions/"):
            if self.submit_calls == 0:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "control_execution_not_found",
                            "message": "missing",
                        }
                    },
                )
            if path == "/internal/v1/executions/" + self.submit_id:
                return httpx.Response(200, json=self.status("succeeded"))
        if request.method == "GET" and path.endswith("/audio-staging-result"):
            return httpx.Response(
                200,
                json={
                    "submit_intent_id": self.submit_id,
                    "request_digest": DIGEST,
                    "staging_result": {
                        "request_digest": DIGEST,
                        "reference": {
                            "asset_or_artifact_id": "artifact-staging",
                            "workspace_id": "workspace-default",
                            "storage_bucket": "synthetic",
                            "object_key": "synthetic/result",
                            "content_sha256": DIGEST,
                            "byte_length": 32,
                        },
                        "storage_version": "version-1",
                        "media_type": "audio/wav",
                    },
                },
            )
        raise AssertionError("unexpected gateway path: " + request.method + " " + path)


def _body() -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic dispatch job"},
        "voice": {"id": "voice-vivian", "revision": "2"},
        "client_reference": "synthetic-dispatch",
    }


async def test_concurrent_claim_executes_one_submission_and_closes_once(
    dispatch_db: DispatchSandbox,
) -> None:
    created = dispatch_db.jobs.submit(
        _body(), request_id="request-dispatch-0001", idempotency_key=None
    )
    gateway, publisher = Gateway(), Publisher()
    client = ExecutionGatewayAdapter(
        GatewayExecutionClient(
            base_url="http://127.0.0.1:9999",
            credential=SecretStr("synthetic-token-0001"),
            timeout_seconds=1,
            max_retries=0,
            backoff_seconds=0,
            _transport=httpx.MockTransport(gateway.handler),
        )
    )
    queue = PostgresDispatchQueue(dispatch_db.pool)
    states = PostgresDispatchStateStore(dispatch_db.pool)
    limits = DispatchLimits(poll_seconds=0.01, job_timeout_seconds=30)
    first = JobDispatcher(queue, states, Planner(), client, publisher, limits=limits)
    second = JobDispatcher(queue, states, Planner(), client, publisher, limits=limits)

    batches = await asyncio.gather(
        first.run_once("worker-synthetic-a"), second.run_once("worker-synthetic-b")
    )

    assert sum(batch.claimed for batch in batches) == 1
    assert sum(batch.succeeded for batch in batches) == 1
    assert gateway.submit_calls == 1 and publisher.calls == 1

    ledger = dispatch_db.query(
        "SELECT a.request_document, a.request_digest, c.request_digest AS coordination_digest, "
        "c.claim_version, c.job_state FROM admission_operations AS a "
        "JOIN coordination_jobs AS c ON c.job_id = a.operation_id "
        "WHERE a.operation_id = %s",
        (created.job_id,),
    )
    assert len(ledger) == 1
    document = ledger[0]["request_document"]
    assert ledger[0]["request_digest"] == ledger[0]["coordination_digest"]
    assert (
        "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()
        == ledger[0]["request_digest"]
    )
    assert json.loads(document)["input"]["text"] == "synthetic dispatch job"
    assert ledger[0]["claim_version"] == 1 and ledger[0]["job_state"] == "succeeded"

    rows = dispatch_db.query(
        "SELECT j.state, j.status_version, j.result_json, c.claim_expires_at_us, "
        "(SELECT count(*) FROM speech_job_events e WHERE e.job_id = j.job_id) AS events "
        "FROM speech_jobs AS j JOIN coordination_jobs AS c ON c.job_id = j.job_id "
        "WHERE j.job_id = %s",
        (created.job_id,),
    )
    assert rows == [
        {
            "state": "succeeded",
            "status_version": 3,
            "result_json": rows[0]["result_json"],
            "claim_expires_at_us": 0,
            "events": 3,
        }
    ]
    result = json.loads(rows[0]["result_json"])
    assert result == {
        "kind": "tts",
        "audio_artifact_id": "artifact-synthetic",
        "receipt_id": "receipt-synthetic",
    }


async def test_cross_lease_takeover_renews_only_the_new_owner(
    dispatch_db: DispatchSandbox,
) -> None:
    created = dispatch_db.jobs.submit(
        _body(), request_id="request-dispatch-0002", idempotency_key=None
    )
    queue = PostgresDispatchQueue(dispatch_db.pool)
    states = PostgresDispatchStateStore(dispatch_db.pool)

    first_claims = await queue.claim_due("worker-synthetic-a", 30_000, 1)
    assert len(first_claims) == 1
    first = replace(first_claims[0], job=await states.mark_running(first_claims[0].job))

    dispatch_db.query(
        "UPDATE coordination_jobs SET claim_expires_at_us = 0 "
        "WHERE job_id = %s AND claim_version = %s",
        (created.job_id, first.claim_version),
    )
    second_claims = await queue.claim_due("worker-synthetic-b", 30_000, 1)
    assert len(second_claims) == 1
    second = second_claims[0]
    assert second.claim_version == 2 and second.job.state == "running"

    assert await states.renew(first, 30_000) is None
    renewed = await states.renew(second, 30_000)
    assert renewed is not None
    result_json = json.dumps(
        {
            "kind": "tts",
            "audio_artifact_id": "artifact-synthetic",
            "receipt_id": "receipt-synthetic",
        },
        separators=(",", ":"),
    )
    terminal = await states.finish(renewed, "succeeded", result_json=result_json, error_code=None)
    assert terminal.state == "succeeded" and terminal.status_version == 3

    with pytest.raises(DispatchClaimLostError):
        await states.finish(first, "succeeded", result_json=result_json, error_code=None)

    rows = dispatch_db.query(
        "SELECT j.state, j.status_version, j.result_json, c.claim_version, "
        "c.job_state, c.claim_expires_at_us, "
        "(SELECT count(*) FROM speech_job_events e WHERE e.job_id = j.job_id) AS events "
        "FROM speech_jobs AS j JOIN coordination_jobs AS c ON c.job_id = j.job_id "
        "WHERE j.job_id = %s",
        (created.job_id,),
    )
    assert rows == [
        {
            "state": "succeeded",
            "status_version": 3,
            "result_json": result_json,
            "claim_version": 2,
            "job_state": "succeeded",
            "claim_expires_at_us": 0,
            "events": 3,
        }
    ]
