"""Real PostgreSQL deletion preview: pinned snapshot, idempotency, and refusals.

The unit tests pin the decisions with a fake transaction; this module pins the
statements. It seeds real ``speech_assets``/``speech_jobs``/``speech_artifacts``
rows through migrations, creates one preview per target through the HTTP route,
and compares the answer with the row the database actually stores:

* the pinned ``target_revision``/``target_digest`` equal the locked target,
* the public dependencies and ``eligible`` derive from live references and the
  Job's own state, not from the request,
* ``expires_at`` is the fixed window and ``plan_digest`` recomputes,
* one key replays one row, and a different request under that key conflicts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
import rfc8785
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.contract.test_architecture_repairs import _deletion_validator
from tests.support.portable_pg import connect as portable_pg_connect
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.job_runtime import _verify_privileges
from speechnexus_backend.speech.application.deletions import SpeechDeletions
from speechnexus_backend.speech.domain.deletion import (
    DeletionTarget,
    idempotency_key_digest,
)
from speechnexus_backend.speech.infrastructure.deletion_plans_postgres import (
    PostgresDeletionPlans,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_reconciliation_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    deletion_plan_grants,
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_backend.speech.infrastructure.voice_draft_schema import voice_draft_grants

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
WORKSPACE = "workspace-default"
ASSET_ID = "asset-plan-pg"
GEN_JOB = "job-plan-gen"
RUN_JOB = "job-plan-run"
CLEAN_JOB = "job-plan-clean"
DRAFT_ID = "draft-plan-pg"
ARTIFACT_ID = "artifact-plan-pg"
NOW_US = 1_760_000_000_000_000
DAY_US = 86_400_000_000
ROLE_PASSWORD = "synthetic_deletion_test"


def sha(letter: str) -> str:
    return "sha256:" + letter * 64


@dataclass(frozen=True)
class Sandbox:
    url: str
    schema: str


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not evidence")
    schema = "sn_del_" + uuid.uuid4().hex[:12]
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
    admin = portable_pg_connect(url, autocommit=True, row_factory=dict_row)
    try:
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        _seed(admin)
        yield Sandbox(url, schema)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _seed(admin: psycopg.Connection[dict[str, Any]]) -> None:
    admin.execute(
        """INSERT INTO speech_assets (asset_id, workspace_id, purpose, asset_state,
            upload_phase, bytes, sha256, request_digest, object_key, media_type,
            duration_ms, sample_rate_hz, channels, created_at_us, expires_at_us, writer_id,
            version)
        VALUES (%s, %s, 'voice_reference', 'ready', 'settled', 1024, %s, %s,
                'assets/asset-plan-pg.wav', 'audio/wav', 1200, 24000, 1, %s, %s, 'writer-1', 3)""",
        (
            ASSET_ID,
            WORKSPACE,
            sha("a"),
            sha("b"),
            NOW_US,
            NOW_US + DAY_US,
        ),
    )
    admin.execute(
        """INSERT INTO speech_asset_objects (asset_id, object_key, storage_version_id,
            bytes, sha256, writer_id, verified_at_us)
        VALUES (%s, 'assets/asset-plan-pg.wav', 'minio-asset-1', 1024, %s, 'writer-1', %s)""",
        (ASSET_ID, sha("a"), NOW_US),
    )
    admin.execute(
        """INSERT INTO speech_asset_references (reference_id, asset_id, consumer_kind,
            consumer_id, consumer_revision, created_at_us)
        VALUES ('ref-plan-pg', %s, 'voice_revision', 'voice-plan-pg', '2', %s)""",
        (ASSET_ID, NOW_US),
    )
    admin.execute(
        """INSERT INTO speech_assets (asset_id, workspace_id, purpose, asset_state,
            upload_phase, bytes, sha256, request_digest, object_key, media_type,
            duration_ms, sample_rate_hz, channels, created_at_us, expires_at_us, version)
        VALUES ('asset-plan-unknown', %s, 'voice_reference', 'ready', 'settled', 512, %s,
                %s, 'assets/asset-plan-unknown.wav', 'audio/wav', 900, 24000, 1, %s, %s, 1)""",
        (WORKSPACE, sha("1"), sha("2"), NOW_US, NOW_US + DAY_US),
    )
    admin.execute(
        """INSERT INTO speech_asset_references (reference_id, asset_id, consumer_kind,
            consumer_id, consumer_revision, created_at_us)
        VALUES ('ref-plan-unknown', 'asset-plan-unknown', 'future_consumer',
                'voice-plan-unknown', '1', %s)""",
        (NOW_US,),
    )
    for job_id, state, status_version, digest in (
        (GEN_JOB, "succeeded", 4, sha("c")),
        (RUN_JOB, "running", 2, sha("d")),
        (CLEAN_JOB, "succeeded", 7, sha("e")),
    ):
        result = '{"synthetic":true}' if state == "succeeded" else None
        admin.execute(
            """INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id,
                request_digest, state, status_version, result_json, created_at_us, updated_at_us)
            VALUES (%s, %s, 'tts', 'tts-synthetic', %s, %s, %s, %s, %s, %s)""",
            (job_id, WORKSPACE, digest, state, status_version, result, NOW_US, NOW_US),
        )
        admin.execute(
            """INSERT INTO speech_job_intents (job_id, request_digest, request_document,
                recorded_at_us)
            VALUES (%s, %s, '{"input":"synthetic"}', %s)""",
            (job_id, digest, NOW_US),
        )
    admin.execute(
        """INSERT INTO speech_voice_drafts (draft_id, generation_job_id, kind, status,
            head_revision, metadata_revision, display_name, input_json, binding_json,
            created_at_us, updated_at_us)
        VALUES (%s, %s, 'design', 'draft', '1', 1, 'Synthetic voice',
                '{"text":"synthetic"}', '{"model":"tts-synthetic"}', %s, %s)""",
        (DRAFT_ID, GEN_JOB, NOW_US, NOW_US),
    )
    admin.execute(
        """INSERT INTO speech_voice_draft_revisions (draft_id, revision, kind,
            generation_job_id, input_json, binding_json, created_at_us)
        VALUES (%s, '1', 'design', %s, '{"text":"synthetic"}', '{"model":"tts-synthetic"}', %s)""",
        (DRAFT_ID, GEN_JOB, NOW_US),
    )
    admin.execute(
        """INSERT INTO speech_artifacts (artifact_id, workspace_id, artifact_kind,
            artifact_state, media_type, bytes, sha256, source_operation_id, object_key,
            storage_version_id, created_at_us, expires_at_us)
        VALUES (%s, %s, 'audio', 'ready', 'audio/wav', 2048, %s, %s,
                'artifacts/artifact-plan-pg.wav', 'minio-artifact-1', %s, %s)""",
        (ARTIFACT_ID, WORKSPACE, sha("f"), GEN_JOB, NOW_US, NOW_US + DAY_US),
    )


def _stored(url: str, schema: str, plan_id: str) -> dict[str, Any]:
    with portable_pg_connect(url, row_factory=dict_row) as connection:
        connection.execute(f'SET search_path TO "{schema}", pg_catalog')
        connection.execute("SET TIME ZONE 'UTC'")
        row = connection.execute(
            "SELECT * FROM speech_deletion_plans WHERE id = %s", (plan_id,)
        ).fetchone()
    assert row is not None, plan_id
    return cast(dict[str, Any], row)


def _rows_for_key(url: str, schema: str, key_digest: str) -> int:
    with portable_pg_connect(url, row_factory=dict_row) as connection:
        connection.execute(f'SET search_path TO "{schema}", pg_catalog')
        connection.execute("SET TIME ZONE 'UTC'")
        row = connection.execute(
            "SELECT count(*) AS total FROM speech_deletion_plans "
            "WHERE workspace_id = %s AND idempotency_key_digest = %s",
            (WORKSPACE, key_digest),
        ).fetchone()
    assert row is not None
    return cast(int, row["total"])


def _key_digest(key: str) -> str:
    return idempotency_key_digest(key)


def _body(kind: str, identifier: str, revision: int, mode: str = "ordinary") -> dict[str, Any]:
    return {
        "target": {
            "owner": "speech",
            "kind": kind,
            "resource": {"id": identifier, "revision": str(revision)},
        },
        "mode": mode,
    }


async def _post(app: Any, body: dict[str, Any], key: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        return await http.post(
            "/v1/nexus/deletion-plans", json=body, headers={"Idempotency-Key": key}
        )


def _app(sandbox: Sandbox) -> tuple[Any, Any]:
    pool = speech_pool(sandbox.url, schema=sandbox.schema)
    pool.open()
    service = SpeechDeletions(PostgresDeletionPlans(pool), workspace_id=WORKSPACE)
    return create_app(settings=_settings(), deletions=service), pool


def _verify_digest(payload: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in payload.items() if key != "plan_digest"}
    canonical = rfc8785.dumps(json.loads(json.dumps(unsigned)))
    assert payload["plan_digest"] == "sha256:" + hashlib.sha256(canonical).hexdigest()


async def test_asset_plan_pins_the_live_reference_and_the_database_snapshot(
    sandbox: Sandbox,
) -> None:
    app, pool = _app(sandbox)
    try:
        response = await _post(app, _body("asset", ASSET_ID, 3), "key-asset-1")
        assert response.status_code == 200, response.text
        payload = response.json()
        _deletion_validator("DeletionPlan").validate(payload)
        _verify_digest(payload)
        stored = _stored(sandbox.url, sandbox.schema, payload["id"])
        assert stored["workspace_id"] == WORKSPACE
        assert stored["target_owner"] == "speech"
        assert stored["target_kind"] == "asset"
        assert stored["target_id"] == ASSET_ID
        # The pinned revision is the locked row's version, not the request's echo.
        assert stored["target_revision"] == "3"
        assert stored["target_digest"] == sha("a")
        assert stored["plan_revision"] == "1"
        assert stored["plan_digest"] == payload["plan_digest"]
        assert stored["dependencies"] == [
            ["speech", "voice_revision", "voice-plan-pg", "2", "persistent_reference", "blocks"]
        ]
        assert payload["dependencies"] == [
            {
                "target": {
                    "owner": "speech",
                    "kind": "voice_revision",
                    "resource": {"id": "voice-plan-pg", "revision": "2"},
                },
                "relation": "persistent_reference",
                "disposition": "blocks",
            }
        ]
        assert payload["eligible"] is False
        window = stored["expires_at"] - stored["created_at"]
        assert window.total_seconds() == 300
        assert payload["expires_at"] == stored["expires_at"].isoformat().replace("+00:00", "Z")
        assert stored["content_snapshot"]["asset_id"] == ASSET_ID
        assert stored["content_snapshot"]["version"] == 3
        assert stored["content_snapshot"]["storage_version_id"] == "minio-asset-1"

        replay = await _post(app, _body("asset", ASSET_ID, 3), "key-asset-1")
        assert replay.status_code == 200
        assert replay.json() == payload
        assert _rows_for_key(sandbox.url, sandbox.schema, _key_digest("key-asset-1")) == 1
    finally:
        pool.close()


async def test_job_plan_pins_the_generation_job_and_its_draft(sandbox: Sandbox) -> None:
    app, pool = _app(sandbox)
    try:
        response = await _post(app, _body("operation_content", GEN_JOB, 4), "key-job-1")
        assert response.status_code == 200, response.text
        payload = response.json()
        _deletion_validator("DeletionPlan").validate(payload)
        stored = _stored(sandbox.url, sandbox.schema, payload["id"])
        assert stored["target_kind"] == "operation_content"
        assert stored["target_revision"] == "4"
        assert stored["target_digest"] == sha("c")
        assert stored["dependencies"] == [
            ["speech", "voice_revision", DRAFT_ID, "1", "persistent_reference", "blocks"]
        ]
        assert payload["eligible"] is False
        assert stored["content_snapshot"]["state"] == "succeeded"
        assert stored["content_snapshot"]["artifacts"][0]["artifact_id"] == ARTIFACT_ID
        assert stored["content_snapshot"]["artifacts"][0]["storage_version_id"] == (
            "minio-artifact-1"
        )
        assert stored["content_snapshot"]["intents"][0]["request_digest"] == sha("c")
    finally:
        pool.close()


async def test_a_running_job_blocks_on_its_own_execution(sandbox: Sandbox) -> None:
    app, pool = _app(sandbox)
    try:
        response = await _post(app, _body("operation_content", RUN_JOB, 2), "key-run-1")
        assert response.status_code == 200, response.text
        payload = response.json()
        stored = _stored(sandbox.url, sandbox.schema, payload["id"])
        assert stored["dependencies"] == [
            ["speech", "operation_content", RUN_JOB, "2", "active_execution", "blocks"]
        ]
        assert payload["eligible"] is False
        assert stored["content_snapshot"]["state"] == "running"
    finally:
        pool.close()


async def test_a_terminal_job_without_references_is_eligible(sandbox: Sandbox) -> None:
    app, pool = _app(sandbox)
    try:
        response = await _post(app, _body("operation_content", CLEAN_JOB, 7), "key-clean-1")
        assert response.status_code == 200, response.text
        payload = response.json()
        stored = _stored(sandbox.url, sandbox.schema, payload["id"])
        assert payload["dependencies"] == []
        assert payload["eligible"] is True
        assert stored["dependencies"] == []
    finally:
        pool.close()


async def test_a_stale_revision_and_a_missing_target_write_nothing(sandbox: Sandbox) -> None:
    app, pool = _app(sandbox)
    try:
        stale = await _post(app, _body("asset", ASSET_ID, 2), "key-stale-1")
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "deletion_plan_changed"
        missing = await _post(app, _body("asset", "asset-plan-absent", 1), "key-missing-1")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "resource_not_found"
        assert _rows_for_key(sandbox.url, sandbox.schema, _key_digest("key-stale-1")) == 0
        assert _rows_for_key(sandbox.url, sandbox.schema, _key_digest("key-missing-1")) == 0
    finally:
        pool.close()


async def test_one_key_replays_the_old_snapshot_after_the_target_moved(
    sandbox: Sandbox,
) -> None:
    app, pool = _app(sandbox)
    try:
        first = await _post(app, _body("asset", ASSET_ID, 3), "key-move-1")
        assert first.status_code == 200
        with portable_pg_connect(sandbox.url, autocommit=True) as connection:
            connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            connection.execute(
                "UPDATE speech_assets SET version = version + 1 WHERE asset_id = %s",
                (ASSET_ID,),
            )
        replay = await _post(app, _body("asset", ASSET_ID, 3), "key-move-1")
        assert replay.status_code == 200
        assert replay.json() == first.json()
        conflict = await _post(app, _body("asset", ASSET_ID, 4), "key-move-1")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        changed = await _post(app, _body("asset", ASSET_ID, 3), "key-move-2")
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "deletion_plan_changed"
        current = await _post(app, _body("asset", ASSET_ID, 4), "key-move-3")
        assert current.status_code == 200
        assert current.json()["target"]["resource"]["revision"] == "4"
        assert _rows_for_key(sandbox.url, sandbox.schema, _key_digest("key-move-1")) == 1
    finally:
        pool.close()


async def test_an_unknown_live_reference_fails_closed(sandbox: Sandbox) -> None:
    app, pool = _app(sandbox)
    try:
        response = await _post(app, _body("asset", "asset-plan-unknown", 1), "key-unknown-1")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "deletion_reference_unsupported"
        assert response.json()["error"]["retryable"] is True
        assert _rows_for_key(sandbox.url, sandbox.schema, _key_digest("key-unknown-1")) == 0
    finally:
        pool.close()


async def test_a_tampered_plan_row_is_not_replayed(sandbox: Sandbox) -> None:
    app, pool = _app(sandbox)
    try:
        first = await _post(app, _body("asset", ASSET_ID, 3), "key-tamper-1")
        assert first.status_code == 200
        with portable_pg_connect(sandbox.url, autocommit=True) as connection:
            connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            # The row is content-addressed: changing any hashed value must stop it
            # from being replayed as if it were the plan the client accepted.
            connection.execute(
                "UPDATE speech_deletion_plans SET dependencies = '[]'::jsonb WHERE id = %s",
                (first.json()["id"],),
            )
        replay = await _post(app, _body("asset", ASSET_ID, 3), "key-tamper-1")
        assert replay.status_code == 503
        assert replay.json()["error"]["code"] == "deletion_plans_unavailable"
    finally:
        pool.close()


async def test_two_connections_with_one_key_commit_one_plan(sandbox: Sandbox) -> None:
    app, pool = _app(sandbox)
    try:
        body = _body("asset", ASSET_ID, 3)

        async def one() -> httpx.Response:
            return await _post(app, body, "key-race-1")

        # Two request tasks on separate pool connections, started together.
        first, second = await asyncio.gather(one(), one())
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] == second.json()["id"]
        assert _rows_for_key(sandbox.url, sandbox.schema, _key_digest("key-race-1")) == 1
    finally:
        pool.close()


def _provision_writer(
    sandbox: Sandbox, *, include_deletion: bool = True
) -> tuple[str, psycopg.Connection[dict[str, Any]]]:
    """Provision exactly the reference writer set, with or without the route's grants."""
    role = "sn_del_writer_" + uuid.uuid4().hex[:8]
    admin = portable_pg_connect(sandbox.url, autocommit=True, row_factory=dict_row)
    admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{ROLE_PASSWORD}'")
    for grant in (
        *job_runtime_grants(sandbox.schema, role),
        *public_job_grants(sandbox.schema, role),
        *voice_draft_grants(sandbox.schema, role),
        *job_dispatch_grants(sandbox.schema, role),
        *cancellation_reconciliation_grants(sandbox.schema, role),
        *public_job_submission_grants(sandbox.schema, role),
        *(deletion_plan_grants(sandbox.schema, role) if include_deletion else ()),
        f'GRANT SELECT ON "{sandbox.schema}".admission_operations, '
        f'"{sandbox.schema}".coordination_jobs TO "{role}"',
        f"GRANT UPDATE (claim_owner_id, claim_expires_at_us, claim_version, job_state) "
        f'ON "{sandbox.schema}".coordination_jobs TO "{role}"',
        f"GRANT SELECT ON speech_workspaces, speech_publications, "
        f"speech_publication_heads, speech_catalog_state, speech_publication_retirements, "
        f"speech_binding_revisions, speech_assets, speech_pronunciation_revisions "
        f'TO "{role}"',
    ):
        admin.execute(grant)
    return role, admin


async def test_the_provisioned_writer_role_can_preview_both_targets(sandbox: Sandbox) -> None:
    """The grant surface is proven by the identity the deployment actually uses."""
    role, admin = _provision_writer(sandbox)
    pool = None
    try:
        with portable_pg_connect(
            make_conninfo(sandbox.url, user=role, password=ROLE_PASSWORD), row_factory=dict_row
        ) as connection:
            connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            # The real startup gate accepts this exact surface.
            _verify_privileges(connection, sandbox.schema)
        pool = speech_pool(
            make_conninfo(sandbox.url, user=role, password=ROLE_PASSWORD),
            schema=sandbox.schema,
        )
        pool.open()
        service = SpeechDeletions(PostgresDeletionPlans(pool), workspace_id=WORKSPACE)
        asset_plan = service.preview(
            DeletionTarget("speech", "asset", ASSET_ID, 3), "ordinary", "key-role-asset"
        )
        job_plan = service.preview(
            DeletionTarget("speech", "operation_content", GEN_JOB, 4),
            "ordinary",
            "key-role-job",
        )
        assert asset_plan.eligible is False
        assert asset_plan.target.revision == 3
        assert job_plan.eligible is False
        assert job_plan.target.revision == 4
        app = create_app(settings=_settings(), deletions=service)
        response = await _post(app, _body("asset", ASSET_ID, 3), "key-role-http")
        assert response.status_code == 200, response.text
        assert response.json()["target"]["resource"]["revision"] == "3"
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


async def test_workspace_is_locked_before_the_asset_like_the_writer_path(
    sandbox: Sandbox,
) -> None:
    """The planner must take the FK parent lock first, or it deadlocks the publisher."""
    app, pool = _app(sandbox)
    started, published = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def publisher() -> None:
        try:
            with portable_pg_connect(sandbox.url, row_factory=dict_row) as connection:
                connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
                connection.execute(
                    "SELECT 1 FROM speech_workspaces WHERE workspace_id = %s FOR UPDATE",
                    (WORKSPACE,),
                )
                started.set()
                time.sleep(0.5)
                connection.execute(
                    "SELECT 1 FROM speech_assets WHERE asset_id = %s FOR UPDATE", (ASSET_ID,)
                )
                connection.commit()
                published.set()
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=publisher)
    thread.start()
    try:
        assert started.wait(5)
        # The publisher holds workspace then asset, the order every asset writer uses.
        response = await _post(app, _body("asset", ASSET_ID, 3), "key-lock-order-1")
        thread.join(10)
        assert failures == []
        assert published.is_set()
        assert response.status_code == 200, response.text
        assert response.json()["target"]["resource"]["revision"] == "3"
    finally:
        thread.join(10)
        pool.close()


async def test_missing_deletion_grants_fail_the_route_not_the_process(
    sandbox: Sandbox,
) -> None:
    """A writer without the route's grants boots, and preview names the gap."""
    role, admin = _provision_writer(sandbox, include_deletion=False)
    pool = None
    try:
        with portable_pg_connect(
            make_conninfo(sandbox.url, user=role, password=ROLE_PASSWORD), row_factory=dict_row
        ) as connection:
            connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            # The process-wide gate accepts this role: deletion is not a boot condition.
            _verify_privileges(connection, sandbox.schema)
        pool = speech_pool(
            make_conninfo(sandbox.url, user=role, password=ROLE_PASSWORD),
            schema=sandbox.schema,
        )
        pool.open()
        service = SpeechDeletions(PostgresDeletionPlans(pool), workspace_id=WORKSPACE)
        app = create_app(settings=_settings(), deletions=service)
        response = await _post(app, _body("asset", ASSET_ID, 3), "key-no-grants")
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["code"] == "deletion_plans_not_configured"
        assert error["retryable"] is False
        row = admin.execute("SELECT count(*) AS n FROM speech_deletion_plans").fetchone()
        assert row is not None and row["n"] == 0
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()
