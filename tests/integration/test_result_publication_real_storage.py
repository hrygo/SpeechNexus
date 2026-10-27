"""Actual publication into PostgreSQL and versioned MinIO, then public readback."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from minio import Minio
from minio.commonconfig import ENABLED
from minio.versioningconfig import VersioningConfig
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.readiness import StaticProbe

from speechnexus_backend.app import create_app
from speechnexus_backend.job_dispatch_runtime import (
    JobDispatchSettings,
    build_result_publisher,
)
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.assets import AssetService
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.application.results import ResultArtifactPublisher
from speechnexus_backend.speech.domain.assets import hash_bytes
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchJob,
    DispatchUnavailableError,
    ExecutionSubmission,
    PublishedResult,
    result_artifact_identity,
    result_object_key,
)
from speechnexus_backend.speech.infrastructure.artifact_schema import (
    artifact_publisher_grants,
    artifact_reader_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
DATABASE_VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
OBJECT_VARIABLES = (
    "SPEECHNEXUS_TEST_MINIO_ENDPOINT",
    "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY",
    "SPEECHNEXUS_TEST_MINIO_SECRET_KEY",
)
WORKSPACE = "workspace-default"
BODY = b'{"text":"synthetic published transcript","language":null,"duration_ms":null}'
DIGEST = hash_bytes(BODY)
JOB_ID = "job-published-0001"
FAILED_JOB_ID = "job-published-failure-0001"
SUBMIT_ID = "submit-published-0001"


@dataclass
class Sandbox:
    admin: psycopg.Connection[dict[str, Any]] = field(repr=False)
    objects: Minio = field(repr=False)
    artifacts_bucket: str
    staging_bucket: str
    staging_version: str
    publisher: ResultArtifactPublisher = field(repr=False)
    client: TestClient = field(repr=False)
    schema: str
    writer_role: str
    reader_role: str
    pool: Any = field(repr=False)

    def row(self, artifact_id: str) -> dict[str, Any] | None:
        with self.admin.cursor() as cursor:
            cursor.execute("SELECT * FROM speech_artifacts WHERE artifact_id = %s", (artifact_id,))
            return cursor.fetchone()

    def versions(self, object_key: str) -> list[Any]:
        return [
            item
            for item in self.objects.list_objects(
                self.artifacts_bucket,
                prefix=object_key,
                recursive=True,
                include_version=True,
            )
            if item.object_name == object_key
        ]


def _required_environment() -> tuple[str, str, str, str]:
    values = [os.environ.get(name) for name in (DATABASE_VARIABLE, *OBJECT_VARIABLES)]
    if not all(values):
        pytest.skip("Configure an isolated PostgreSQL DSN and MinIO test endpoint/keys")
    return (
        cast(str, values[0]),
        cast(str, values[1]),
        cast(str, values[2]),
        cast(str, values[3]),
    )


def _migrate(url: str, schema: str) -> None:
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


def _job(job_id: str) -> DispatchJob:
    return DispatchJob(
        job_id,
        WORKSPACE,
        "asr",
        "running",
        2,
        1_700_000_000_000_000,
        "sha256:" + "a" * 64,
        '{"kind":"asr"}',
    )


def _submission() -> ExecutionSubmission:
    return ExecutionSubmission(SUBMIT_ID, {"submit_intent_id": SUBMIT_ID})


def _receipt(staging_key: str, storage_version: str) -> dict[str, Any]:
    return {
        "request_digest": "sha256:" + "a" * 64,
        "reference": {
            "asset_or_artifact_id": "artifact-staging-1",
            "workspace_id": WORKSPACE,
            "storage_bucket": "placeholder",
            "object_key": staging_key,
            "content_sha256": DIGEST,
            "byte_length": len(BODY),
        },
        "storage_version": storage_version,
        "media_type": "application/json",
    }


def _dispatch_settings(
    url: str,
    schema: str,
    endpoint: str,
    artifacts_bucket: str,
    staging_bucket: str,
    access: str,
    secret: str,
) -> JobDispatchSettings:
    return JobDispatchSettings(
        process_role="job-worker",
        node_id="node-result-test",
        instance_id="worker-result-test",
        database_url=SecretStr(url),
        database_schema=schema,
        gateway_base_url="http://127.0.0.1:8010",
        gateway_credential=SecretStr("synthetic-gateway-token"),
        gateway_timeout_seconds=1.0,
        batch_size=1,
        lease_ms=30_000,
        poll_seconds=0.01,
        job_timeout_seconds=600.0,
        max_attempts=5,
        interval_seconds=1.0,
        storage_timeout_seconds=3.0,
        object_storage_endpoint=AnyHttpUrl(endpoint),
        artifacts_bucket=artifacts_bucket,
        execution_result_bucket=staging_bucket,
        object_storage_access_key=SecretStr(access),
        object_storage_secret_key=SecretStr(secret),
    )


def _reader_settings(
    url: str,
    schema: str,
    role: str,
    endpoint: str,
    artifacts_bucket: str,
    access: str,
    secret: str,
) -> Settings:
    return Settings(
        environment="test",
        process_role="job-worker",
        node_id="node-result-reader",
        instance_id="worker-result-reader",
        database_url=SecretStr(url),
        database_schema=schema,
        object_storage_endpoint=AnyHttpUrl(endpoint),
        object_storage_healthcheck_url=AnyHttpUrl(endpoint.rstrip("/") + "/minio/health/live"),
        object_storage_bucket=artifacts_bucket,
        object_storage_access_key=SecretStr(access),
        object_storage_secret_key=SecretStr(secret),
        artifact_reader_database_url=SecretStr(make_conninfo(url, user=role)),
        capability_default_workspace_id=WORKSPACE,
    )


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url, endpoint, access, secret = _required_environment()
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_result_int_" + suffix
    writer_role, reader_role = "sn_result_writer_" + suffix, "sn_result_reader_" + suffix
    artifacts_bucket, staging_bucket = "sn-artifacts-" + suffix, "sn-results-" + suffix
    _migrate(url, schema)
    admin = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    objects = Minio(
        endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=access,
        secret_key=secret,
        secure=endpoint.startswith("https://"),
        region="us-east-1",
    )
    pool = None
    publisher = None
    try:
        admin.execute(f'CREATE ROLE "{writer_role}" LOGIN')
        admin.execute(f'CREATE ROLE "{reader_role}" LOGIN')
        for grant in artifact_publisher_grants(schema, writer_role):
            admin.execute(grant)
        for grant in artifact_reader_grants(schema, reader_role):
            admin.execute(grant)
        objects.make_bucket(artifacts_bucket)
        objects.set_bucket_versioning(artifacts_bucket, VersioningConfig(ENABLED))
        objects.make_bucket(staging_bucket)
        objects.set_bucket_versioning(staging_bucket, VersioningConfig(ENABLED))
        written = objects.put_object(
            staging_bucket,
            "staging/result.json",
            io.BytesIO(BODY),
            len(BODY),
            content_type="application/json",
        )
        staging_version = written.version_id
        assert staging_version is not None
        settings = _dispatch_settings(
            url, schema, endpoint, artifacts_bucket, staging_bucket, access, secret
        )
        pool = speech_pool(
            make_conninfo(url, user=writer_role),
            schema=schema,
            max_size=2,
            name="result-" + suffix,
        )
        pool.open()
        publisher = build_result_publisher(settings, pool)
        app = create_app(
            settings=_reader_settings(
                url, schema, reader_role, endpoint, artifacts_bucket, access, secret
            ),
            assets=cast(AssetService, object()),
            readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
        )
        with TestClient(app) as client:
            yield Sandbox(
                admin,
                objects,
                artifacts_bucket,
                staging_bucket,
                staging_version,
                publisher,
                client,
                schema,
                writer_role,
                reader_role,
                pool,
            )
    finally:
        if publisher is not None:
            publisher.close()
        if pool is not None:
            pool.close()
        for bucket in (artifacts_bucket, staging_bucket):
            for item in objects.list_objects(bucket, recursive=True, include_version=True):
                objects.remove_object(bucket, item.object_name, version_id=item.version_id)
            objects.remove_bucket(bucket)
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{writer_role}"')
        admin.execute(f'DROP ROLE IF EXISTS "{reader_role}"')
        admin.close()


def _receipt_for(sandbox: Sandbox) -> dict[str, Any]:
    receipt = _receipt("staging/result.json", sandbox.staging_version)
    receipt["reference"]["storage_bucket"] = sandbox.staging_bucket
    return receipt


def _publish(sandbox: Sandbox, job_id: str) -> PublishedResult:
    return asyncio.run(
        sandbox.publisher.publish(_job(job_id), _submission(), _receipt_for(sandbox))
    )


def _object_body(sandbox: Sandbox, row: dict[str, Any]) -> bytes:
    response = sandbox.objects.get_object(
        sandbox.artifacts_bucket,
        row["object_key"],
        version_id=row["storage_version_id"],
    )
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def test_real_pg_minio_publication_readback_and_failure_recovery(sandbox: Sandbox) -> None:
    published = _publish(sandbox, JOB_ID)
    row = sandbox.row(published.artifact_id)
    assert row is not None
    assert row["artifact_kind"] == "transcript"
    assert row["artifact_state"] == "ready"
    assert row["bytes"] == len(BODY)
    assert row["sha256"] == DIGEST
    assert row["receipt_id"] == published.receipt_id
    assert _object_body(sandbox, row) == BODY
    assert len(sandbox.versions(row["object_key"])) == 1
    print(
        "CHECK 1 rows=1 object_versions=1 "
        f"artifact_id={row['artifact_id']} bytes={row['bytes']} sha256={row['sha256']}"
    )

    metadata = sandbox.client.get(f"/v1/nexus/artifacts/{published.artifact_id}")
    assert metadata.status_code == 200
    assert metadata.json()["status"] == "ready"
    assert metadata.json()["bytes"] == len(BODY)
    assert metadata.json()["sha256"] == DIGEST
    print(
        "CHECK 2 status="
        f"{metadata.json()['status']} bytes={metadata.json()['bytes']} "
        f"sha256={metadata.json()['sha256']}"
    )

    content = sandbox.client.get(f"/v1/nexus/artifacts/{published.artifact_id}/content")
    assert content.status_code == 200
    assert content.content == BODY
    content_digest = "sha256:" + hashlib.sha256(content.content).hexdigest()
    assert content_digest == row["sha256"]
    print(f"CHECK 3 content_bytes={len(content.content)} content_sha256={content_digest}")

    repeated = _publish(sandbox, JOB_ID)
    assert repeated == published
    duplicate = sandbox.row(published.artifact_id)
    assert duplicate is not None and duplicate == row
    assert _object_body(sandbox, duplicate) == BODY
    assert len(sandbox.versions(row["object_key"])) == 1
    print("CHECK 4 repeat_rows=1 object_versions=1 content_unchanged=true")

    sandbox.admin.execute("CREATE SEQUENCE result_fail_once_seq")
    sandbox.admin.execute(
        "CREATE FUNCTION result_fail_once() RETURNS trigger LANGUAGE plpgsql AS $$"
        " BEGIN IF nextval('result_fail_once_seq') = 1 THEN"
        " RAISE EXCEPTION 'synthetic artifact insert failure';"
        " END IF; RETURN NEW; END $$"
    )
    sandbox.admin.execute(
        "CREATE TRIGGER result_fail_once BEFORE INSERT ON speech_artifacts "
        "FOR EACH ROW EXECUTE FUNCTION result_fail_once()"
    )
    sandbox.admin.execute(
        f'GRANT USAGE ON SEQUENCE result_fail_once_seq TO "{sandbox.writer_role}"'
    )

    with pytest.raises(DispatchUnavailableError):
        _publish(sandbox, FAILED_JOB_ID)

    failed_id = result_artifact_identity(FAILED_JOB_ID, "asr")
    failed_key = result_object_key(WORKSPACE, failed_id)
    assert sandbox.row(failed_id) is None
    assert len(sandbox.versions(failed_key)) == 1

    recovered = _publish(sandbox, FAILED_JOB_ID)
    recovered_row = sandbox.row(recovered.artifact_id)
    assert recovered.artifact_id == failed_id
    assert recovered_row is not None
    assert recovered_row["artifact_state"] == "ready"
    assert _object_body(sandbox, recovered_row) == BODY
    assert len(sandbox.versions(failed_key)) == 1
    with sandbox.admin.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS count FROM speech_artifacts")
        count_row = cursor.fetchone()
        assert count_row is not None
        assert count_row["count"] == 2
    print("CHECK 5 failed_db_rows=1 object_versions=1 retry_rows=1 retry_object_versions=1")
