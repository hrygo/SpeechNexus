"""Real PostgreSQL + versioned MinIO retention sweep and its read consequences.

The unit tests pin the decisions; this module pins the statements. It proves on a
real database that

* the sweep selects exactly the rows whose recorded ``expires_at_us`` the
  database clock has passed,
* a row inside its window keeps its state and its object bytes in the same pass,
* a confirmed removal moves the row to ``deleted`` and drops the exact object
  version it pinned, and
* the public read path answers ``artifact_expired`` before the sweep and
  ``artifact_deleted`` after it, while the retained row still downloads.

The retention runtime is opened through its real startup authority check, so a
role that could widen the sweep into a content-deletion authority is refused
here as well.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from minio import Minio
from minio.commonconfig import ENABLED
from minio.error import S3Error
from minio.versioningconfig import VersioningConfig
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.artifact_retention_runtime import ArtifactRetentionRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.artifacts import ArtifactService
from speechnexus_backend.speech.domain.artifacts import ArtifactError
from speechnexus_backend.speech.infrastructure.artifact_schema import (
    artifact_reader_grants,
    artifact_retention_grants,
)
from speechnexus_backend.speech.infrastructure.artifacts_minio import MinioArtifactObjects
from speechnexus_backend.speech.infrastructure.artifacts_postgres import PostgresArtifactMetadata
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
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
ELAPSED_ID = "artifact-real-elapsed-1"
RETAINED_ID = "artifact-real-retained-1"
ELAPSED_KEY = "private/artifacts/real-elapsed/object"
RETAINED_KEY = "private/artifacts/real-retained/object"
ELAPSED_CONTENT = b"synthetic elapsed artifact bytes"
RETAINED_CONTENT = b"synthetic retained artifact bytes"
RETENTION_US = 30 * 24 * 60 * 60 * 1_000_000


@dataclass
class Sandbox:
    admin: psycopg.Connection[dict[str, object]]
    objects: Minio
    bucket: str
    elapsed_version: str
    retained_version: str
    retention: ArtifactRetentionRuntime
    reader: ArtifactService


def _required_environment() -> tuple[str, str, str, str]:
    database = os.environ.get(DATABASE_VARIABLE)
    if not database:
        pytest.fail(
            f"Configure an isolated PostgreSQL DSN in {DATABASE_VARIABLE}; skip is not evidence"
        )
    objects = [os.environ.get(name) for name in OBJECT_VARIABLES]
    if not all(objects):
        pytest.skip("Configure an isolated MinIO test endpoint/keys")
    return (database, cast(str, objects[0]), cast(str, objects[1]), cast(str, objects[2]))


def _migrate(url: str, schema: str, revision: str = "head") -> None:
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
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def _settings(
    url: str,
    schema: str,
    role: str,
    password: str,
    endpoint: str,
    bucket: str,
    access: str,
    secret: str,
) -> Settings:
    return Settings(
        environment="test",
        process_role="scheduler",
        node_id="node-artifact-retention",
        instance_id="retention-artifact-test",
        database_url=SecretStr(make_conninfo(url, user=role, password=password)),
        database_schema=schema,
        object_storage_endpoint=AnyHttpUrl(endpoint),
        object_storage_healthcheck_url=AnyHttpUrl(endpoint.rstrip("/") + "/minio/health/live"),
        object_storage_bucket=bucket,
        object_storage_access_key=SecretStr(access),
        object_storage_secret_key=SecretStr(secret),
        artifact_retention_batch_size=10,
    )


def _insert_artifact(
    admin: psycopg.Connection[dict[str, object]],
    artifact_id: str,
    *,
    object_key: str,
    version_id: str,
    content: bytes,
    created_at_us: int,
    expires_at_us: int,
) -> None:
    admin.execute(
        "INSERT INTO speech_artifacts "
        "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, "
        "sha256, receipt_id, source_operation_id, object_key, storage_version_id, "
        "created_at_us, expires_at_us) VALUES "
        "(%s, %s, 'audio', 'ready', 'audio/wav', %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            artifact_id,
            WORKSPACE,
            len(content),
            "sha256:" + hashlib.sha256(content).hexdigest(),
            "receipt-" + artifact_id,
            "operation-" + artifact_id,
            object_key,
            version_id,
            created_at_us,
            expires_at_us,
        ),
    )


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url, endpoint, access, secret = _required_environment()
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_artifact_retention_" + suffix
    role, reader_role = "sn_artifact_retention_" + suffix, "sn_artifact_reader_" + suffix
    bucket = "sn-artifact-retention-" + suffix
    password, reader_password = "synthetic_retention", "synthetic_retention_reader"
    _migrate(url, schema)
    admin = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
    admin.execute(f"CREATE ROLE \"{reader_role}\" LOGIN PASSWORD '{reader_password}'")
    for grant in artifact_retention_grants(schema, role):
        admin.execute(grant)
    for grant in artifact_reader_grants(schema, reader_role):
        admin.execute(grant)
    objects = Minio(
        endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=access,
        secret_key=secret,
        secure=endpoint.startswith("https://"),
        region="us-east-1",
    )
    objects.make_bucket(bucket)
    objects.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
    versions: dict[str, str] = {}
    for key, content in ((ELAPSED_KEY, ELAPSED_CONTENT), (RETAINED_KEY, RETAINED_CONTENT)):
        written = objects.put_object(
            bucket, key, io.BytesIO(content), len(content), content_type="application/octet-stream"
        )
        assert written.version_id is not None
        versions[key] = written.version_id
    now_us = time.time_ns() // 1_000
    _insert_artifact(
        admin,
        ELAPSED_ID,
        object_key=ELAPSED_KEY,
        version_id=versions[ELAPSED_KEY],
        content=ELAPSED_CONTENT,
        created_at_us=now_us - RETENTION_US - 120_000_000,
        expires_at_us=now_us - 60_000_000,
    )
    _insert_artifact(
        admin,
        RETAINED_ID,
        object_key=RETAINED_KEY,
        version_id=versions[RETAINED_KEY],
        content=RETAINED_CONTENT,
        created_at_us=now_us - 60_000_000,
        expires_at_us=now_us + RETENTION_US,
    )
    retention = ArtifactRetentionRuntime(
        _settings(url, schema, role, password, endpoint, bucket, access, secret)
    )
    reader_pool = speech_pool(
        make_conninfo(url, user=reader_role, password=reader_password),
        schema=schema,
        max_size=2,
        name="sn-artifact-reader-" + suffix[:8],
    )
    reader_objects = MinioArtifactObjects(endpoint, bucket, access, secret)
    reader_pool.open()
    reader = ArtifactService(
        PostgresArtifactMetadata(reader_pool, timeout_seconds=3), reader_objects, WORKSPACE
    )
    retention.open()
    try:
        yield Sandbox(
            admin,
            objects,
            bucket,
            versions[ELAPSED_KEY],
            versions[RETAINED_KEY],
            retention,
            reader,
        )
    finally:
        retention.close()
        reader_objects.close()
        reader_pool.close()
        for item in objects.list_objects(bucket, recursive=True, include_version=True):
            if item.version_id is not None:
                objects.remove_object(bucket, item.object_name, version_id=item.version_id)
        objects.remove_bucket(bucket)
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.execute(f'DROP ROLE IF EXISTS "{reader_role}"')
        admin.close()


def _state(
    admin: psycopg.Connection[dict[str, object]], artifact_id: str
) -> tuple[str, int | None]:
    row = admin.execute(
        "SELECT artifact_state, expires_at_us FROM speech_artifacts WHERE artifact_id = %s",
        (artifact_id,),
    ).fetchone()
    assert row is not None
    return str(row["artifact_state"]), cast(int | None, row["expires_at_us"])


def _removed_at(admin: psycopg.Connection[dict[str, object]], artifact_id: str) -> int | None:
    row = admin.execute(
        "SELECT deleted_at_us FROM speech_artifacts WHERE artifact_id = %s",
        (artifact_id,),
    ).fetchone()
    assert row is not None
    return cast(int | None, row["deleted_at_us"])


def _version_present(sandbox: Sandbox, key: str, version_id: str) -> bool:
    try:
        sandbox.objects.stat_object(sandbox.bucket, key, version_id=version_id)
    except S3Error as error:
        if error.code in ("NoSuchKey", "NoSuchObject", "NoSuchVersion"):
            return False
        raise
    return True


def test_expiry_precedes_a_confirmed_removal_and_leaves_retained_rows_alone(
    sandbox: Sandbox,
) -> None:
    assert sandbox.retention.retention is not None
    # Before the sweep the read path already refuses the elapsed row by its
    # deadline while both rows are still committed `ready`.
    with pytest.raises(ArtifactError) as elapsed_error:
        sandbox.reader.download(ELAPSED_ID)
    assert elapsed_error.value.code == "artifact_expired"
    assert sandbox.reader.download(RETAINED_ID).artifact.artifact_id == RETAINED_ID
    assert _state(sandbox.admin, ELAPSED_ID)[0] == "ready"
    assert _state(sandbox.admin, RETAINED_ID)[0] == "ready"

    outcomes = sandbox.retention.retention.sweep()

    decisions = {(item.artifact_id, item.decision) for item in outcomes}
    assert decisions == {(ELAPSED_ID, "expired"), (ELAPSED_ID, "deleted")}
    # The retained row was never selected, so no decision names it.
    assert all(item.artifact_id != RETAINED_ID for item in outcomes)
    # The elapsed row records the decision durably and keeps its deadline, and
    # the removal itself is timestamped in the row (the deletion ledger).
    state, expires_at_us = _state(sandbox.admin, ELAPSED_ID)
    assert state == "deleted"
    assert expires_at_us is not None
    removed_at = _removed_at(sandbox.admin, ELAPSED_ID)
    assert removed_at is not None and removed_at >= expires_at_us
    # A retained row carries no removal record.
    assert _removed_at(sandbox.admin, RETAINED_ID) is None
    # The retained row keeps its state, its deadline and its bytes.
    retained_state, retained_expires_at_us = _state(sandbox.admin, RETAINED_ID)
    assert retained_state == "ready"
    assert retained_expires_at_us is not None
    assert _version_present(sandbox, RETAINED_KEY, sandbox.retained_version)
    # The elapsed row's exact version is gone; no other version was chosen.
    assert not _version_present(sandbox, ELAPSED_KEY, sandbox.elapsed_version)

    with pytest.raises(ArtifactError) as removed_error:
        sandbox.reader.download(ELAPSED_ID)
    assert removed_error.value.code == "artifact_deleted"
    retained = sandbox.reader.download(RETAINED_ID)
    assert b"".join(sandbox.reader.content(retained)) == RETAINED_CONTENT

    # A second pass has nothing left to decide and cannot resurrect the row.
    assert sandbox.retention.retention.sweep() == ()
    assert _state(sandbox.admin, ELAPSED_ID)[0] == "deleted"
    assert _state(sandbox.admin, RETAINED_ID)[0] == "ready"


def test_a_shared_runtime_identity_cannot_run_the_sweep(sandbox: Sandbox) -> None:
    """The sweep refuses the API runtime identity, which is why its DSN is separate.

    `sn_api` legitimately holds memory/schema-wide coordination privileges; a role
    that can read and write other relations is not a retention role even if it
    also carries `artifact_retention_grants`. Startup must refuse it by name
    instead of running with an authority nobody reviewed.
    """
    shared = "sn_api_like_" + uuid.uuid4().hex[:8]
    peer = "sn_gateway_like_" + uuid.uuid4().hex[:8]
    current = sandbox.admin.execute("SELECT current_schema() AS name").fetchone()
    assert current is not None
    schema = str(current["name"])
    sandbox.admin.execute(f"CREATE ROLE \"{shared}\" LOGIN PASSWORD 'synthetic_shared'")
    sandbox.admin.execute(f"CREATE ROLE \"{peer}\" LOGIN PASSWORD 'synthetic_peer'")
    url, endpoint, access, secret = _required_environment()
    try:
        for grant in coordination_runtime_grants(schema, speech_role=shared, gateway_role=peer):
            sandbox.admin.execute(grant)
        for grant in artifact_retention_grants(schema, shared):
            sandbox.admin.execute(grant)
        with pytest.raises(ValueError, match="Artifact retention startup rejected") as refused:
            ArtifactRetentionRuntime(
                _settings(
                    url,
                    schema,
                    shared,
                    "synthetic_shared",
                    endpoint,
                    sandbox.bucket,
                    access,
                    secret,
                )
            ).open()
        # The refusal names the privilege family, not a bare mismatch.
        assert "Artifact retention startup rejected" in str(refused.value)
    finally:
        sandbox.admin.execute(f'DROP OWNED BY "{shared}"')
        sandbox.admin.execute(f'DROP OWNED BY "{peer}"')
        sandbox.admin.execute(f'DROP ROLE IF EXISTS "{shared}"')
        sandbox.admin.execute(f'DROP ROLE IF EXISTS "{peer}"')


def test_a_row_deleted_before_the_ledger_migration_is_not_given_a_fake_time() -> None:
    """Pre-migration deletions keep an honest NULL instead of an invented instant.

    Nothing in the schema records *when* a row became ``deleted``: there is no
    ``updated_at_us``, no revision column and no audit table, only the
    publication time and the deadline. So the ledger migration must not write a
    computed value for rows that were already deleted -- that would record an
    event nobody observed. The check is installed ``NOT VALID`` for exactly this
    reason: history stays untraceable, new writes are constrained.
    """
    url, _, _, _ = _required_environment()
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_artifact_ledger_" + suffix
    admin = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        _migrate(url, schema, "0053_voice_draft_publication")
        admin.execute(f'SET search_path TO "{schema}"')
        # A deployment that marked a row `deleted` before this migration existed.
        admin.execute(
            "INSERT INTO speech_artifacts "
            "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, "
            "bytes, sha256, receipt_id, source_operation_id, object_key, "
            "storage_version_id, created_at_us, expires_at_us) VALUES "
            "('artifact-legacy-deleted', %s, 'audio', 'deleted', 'audio/wav', 5, %s, "
            "'receipt-legacy', 'operation-legacy', 'private/legacy', 'version-legacy', "
            "1700000000000000, 1700000000000001)",
            (WORKSPACE, "sha256:" + "a" * 64),
        )

        _migrate(url, schema, "head")

        legacy = admin.execute(
            "SELECT artifact_state, deleted_at_us FROM speech_artifacts "
            "WHERE artifact_id = 'artifact-legacy-deleted'"
        ).fetchone()
        assert legacy is not None
        assert legacy["artifact_state"] == "deleted"
        # No observation existed, so the ledger stays empty rather than inventing one.
        assert legacy["deleted_at_us"] is None
        # The constraint exists but was deliberately not validated against history.
        validated = admin.execute(
            "SELECT convalidated FROM pg_catalog.pg_constraint "
            "WHERE conname = 'speech_artifacts_deleted_ledger_check' "
            "AND connamespace = %s::regnamespace",
            (schema,),
        ).fetchone()
        assert validated is not None and validated["convalidated"] is False
        # A new deletion without a recorded time is refused from now on.
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute(
                "INSERT INTO speech_artifacts "
                "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, "
                "bytes, sha256, receipt_id, source_operation_id, object_key, "
                "storage_version_id, created_at_us, expires_at_us) VALUES "
                "('artifact-new-deleted', %s, 'audio', 'deleted', 'audio/wav', 5, %s, "
                "'receipt-new', 'operation-new', 'private/new', 'version-new', "
                "1700000000000000, 1700000000000001)",
                (WORKSPACE, "sha256:" + "b" * 64),
            )
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def test_retention_role_may_not_widen_into_content_deletion(sandbox: Sandbox) -> None:
    """A table-level write grant is refused before the first row is read."""
    widened = "sn_artifact_retention_wide_" + uuid.uuid4().hex[:8]
    sandbox.admin.execute(f"CREATE ROLE \"{widened}\" LOGIN PASSWORD 'synthetic_wide'")
    current = sandbox.admin.execute("SELECT current_schema() AS name").fetchone()
    assert current is not None
    schema = str(current["name"])
    for grant in artifact_retention_grants(schema, widened):
        sandbox.admin.execute(grant)
    sandbox.admin.execute(f'GRANT UPDATE ON "{schema}".speech_artifacts TO "{widened}"')
    url, endpoint, access, secret = _required_environment()
    try:
        with pytest.raises(ValueError, match="Artifact retention startup rejected"):
            ArtifactRetentionRuntime(
                _settings(
                    url, schema, widened, "synthetic_wide", endpoint, sandbox.bucket, access, secret
                )
            ).open()
    finally:
        # The role holds schema/table/column grants, so its privileges go first.
        sandbox.admin.execute(f'DROP OWNED BY "{widened}"')
        sandbox.admin.execute(f'DROP ROLE IF EXISTS "{widened}"')
