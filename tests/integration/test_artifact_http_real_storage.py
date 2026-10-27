"""Actual HTTP -> read-only PostgreSQL -> versioned MinIO artifact reads."""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.assets import AssetService
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.infrastructure.artifact_schema import artifact_reader_grants

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
DATABASE_VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
OBJECT_VARIABLES = (
    "SPEECHNEXUS_TEST_MINIO_ENDPOINT",
    "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY",
    "SPEECHNEXUS_TEST_MINIO_SECRET_KEY",
)
PASSWORD = "synthetic_artifact_reader"
WORKSPACE = "workspace-default"
OTHER_WORKSPACE = "workspace-other"
CONTENT = b"synthetic artifact bytes 0123456789"
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()
ARTIFACT_ID = "artifact-real-source-1"
OTHER_ARTIFACT_ID = "artifact-real-foreign-1"
EXPIRED_ARTIFACT_ID = "artifact-real-expired-1"
PAST_ARTIFACT_ID = "artifact-real-past-1"
DELETED_ARTIFACT_ID = "artifact-real-deleted-1"
OBJECT_KEY = "private/artifacts/real-source/object"


@dataclass
class Sandbox:
    admin: psycopg.Connection[dict[str, object]]
    objects: Minio
    bucket: str
    version_id: str
    client: TestClient


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


def _insert_artifact(
    admin: psycopg.Connection[dict[str, object]],
    artifact_id: str,
    *,
    workspace_id: str = WORKSPACE,
    status: str = "ready",
    object_key: str,
    version_id: str,
    expires_at_us: int | None = None,
) -> None:
    admin.execute(
        "INSERT INTO speech_artifacts "
        "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, "
        "sha256, receipt_id, source_operation_id, object_key, storage_version_id, "
        "created_at_us, expires_at_us) VALUES "
        "(%s, %s, 'audio', %s, 'audio/wav', %s, %s, 'receipt-real-1', "
        "'operation-real-1', %s, %s, 1700000000000000, %s)",
        (
            artifact_id,
            workspace_id,
            status,
            len(CONTENT),
            DIGEST,
            object_key,
            version_id,
            expires_at_us,
        ),
    )


def _settings(
    url: str, schema: str, role: str, endpoint: str, bucket: str, access: str, secret: str
) -> Settings:
    reader_url = make_conninfo(url, user=role, password=PASSWORD)
    return Settings(
        environment="test",
        process_role="job-worker",
        node_id="node-artifact-test",
        instance_id="worker-artifact-test",
        database_url=SecretStr(url),
        database_schema=schema,
        object_storage_endpoint=AnyHttpUrl(endpoint),
        object_storage_healthcheck_url=AnyHttpUrl(endpoint.rstrip("/") + "/minio/health/live"),
        object_storage_bucket=bucket,
        object_storage_access_key=SecretStr(access),
        object_storage_secret_key=SecretStr(secret),
        artifact_reader_database_url=SecretStr(reader_url),
        capability_default_workspace_id=WORKSPACE,
    )


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url, endpoint, access, secret = _required_environment()
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_artifact_int_" + suffix, "sn_artifact_reader_" + suffix
    bucket = "sn-artifacts-" + suffix
    _migrate(url, schema)
    admin = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    for grant in artifact_reader_grants(schema, role):
        admin.execute(grant)
    admin.execute(
        "INSERT INTO speech_workspaces "
        "(workspace_id, display_name, workspace_state, created_at_us) "
        "VALUES (%s, 'Synthetic other workspace', 'active', 1700000000000000)",
        (OTHER_WORKSPACE,),
    )
    objects = Minio(
        endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=access,
        secret_key=secret,
        secure=endpoint.startswith("https://"),
        region="us-east-1",
    )
    objects.make_bucket(bucket)
    objects.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
    written = objects.put_object(
        bucket,
        OBJECT_KEY,
        io.BytesIO(CONTENT),
        len(CONTENT),
        content_type="application/octet-stream",
    )
    assert written.version_id is not None
    version_id = written.version_id
    _insert_artifact(admin, ARTIFACT_ID, object_key=OBJECT_KEY, version_id=version_id)
    _insert_artifact(
        admin,
        OTHER_ARTIFACT_ID,
        workspace_id=OTHER_WORKSPACE,
        object_key="private/artifacts/foreign/object",
        version_id="version-foreign-1",
    )
    _insert_artifact(
        admin,
        EXPIRED_ARTIFACT_ID,
        status="expired",
        object_key="private/artifacts/expired/object",
        version_id="version-expired-1",
    )
    _insert_artifact(
        admin,
        PAST_ARTIFACT_ID,
        object_key="private/artifacts/past/object",
        version_id="version-past-1",
        expires_at_us=1_700_000_000_000_001,
    )
    _insert_artifact(
        admin,
        DELETED_ARTIFACT_ID,
        status="deleted",
        object_key="private/artifacts/deleted/object",
        version_id="version-deleted-1",
    )
    app = create_app(
        settings=_settings(url, schema, role, endpoint, bucket, access, secret),
        assets=cast(AssetService, object()),
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    try:
        with TestClient(app) as client:
            yield Sandbox(admin, objects, bucket, version_id, client)
    finally:
        for item in objects.list_objects(bucket, recursive=True, include_version=True):
            objects.remove_object(bucket, item.object_name, version_id=item.version_id)
        objects.remove_bucket(bucket)
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def _assert_private_storage_absent(text: str, bucket: str, endpoint: str) -> None:
    for value in (bucket, OBJECT_KEY, endpoint, "MinIO", "S3Error", "NoSuchKey", "NoSuchVersion"):
        assert value not in text


def test_real_pg_and_minio_download_semantics(sandbox: Sandbox) -> None:
    path = f"/v1/nexus/artifacts/{ARTIFACT_ID}"
    content_path = path + "/content"

    metadata = sandbox.client.get(path)
    assert metadata.status_code == 200
    assert metadata.json()["id"] == ARTIFACT_ID
    assert metadata.json()["bytes"] == len(CONTENT)
    assert metadata.json()["sha256"] == DIGEST
    assert metadata.json()["status"] == "ready"

    whole = sandbox.client.get(content_path)
    assert whole.status_code == 200
    assert whole.content == CONTENT
    assert whole.headers["Content-Length"] == str(len(CONTENT))
    assert whole.headers["ETag"] == f'"{DIGEST}"'
    assert whole.headers["Content-Disposition"] == f'attachment; filename="{ARTIFACT_ID}.wav"'

    not_modified = sandbox.client.get(
        content_path, headers={"If-None-Match": whole.headers["ETag"]}
    )
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["ETag"] == whole.headers["ETag"]

    partial = sandbox.client.get(content_path, headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert partial.content == CONTENT[:4]
    assert partial.headers["Content-Range"] == f"bytes 0-3/{len(CONTENT)}"
    assert partial.headers["Content-Length"] == "4"

    unsatisfiable = sandbox.client.get(content_path, headers={"Range": f"bytes={len(CONTENT)}-"})
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["Content-Range"] == f"bytes */{len(CONTENT)}"
    assert unsatisfiable.json()["error"]["code"] == "artifact_range_not_satisfiable"

    unknown = sandbox.client.get("/v1/nexus/artifacts/artifact-real-missing-1/content")
    foreign = sandbox.client.get(f"/v1/nexus/artifacts/{OTHER_ARTIFACT_ID}/content")
    for response in (unknown, foreign):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "artifact_not_found"

    expired = sandbox.client.get(f"/v1/nexus/artifacts/{EXPIRED_ARTIFACT_ID}/content")
    past = sandbox.client.get(f"/v1/nexus/artifacts/{PAST_ARTIFACT_ID}/content")
    deleted = sandbox.client.get(f"/v1/nexus/artifacts/{DELETED_ARTIFACT_ID}/content")
    for response in (expired, past, deleted):
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "artifact_unavailable"

    sandbox.objects.remove_object(sandbox.bucket, OBJECT_KEY, version_id=sandbox.version_id)
    missing_version = sandbox.client.get(content_path)
    assert missing_version.status_code == 410
    assert missing_version.json()["error"]["code"] == "artifact_unavailable"

    errors = (unsatisfiable, unknown, foreign, expired, past, deleted, missing_version)
    for response in errors:
        _assert_private_storage_absent(
            response.text, sandbox.bucket, os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"]
        )
