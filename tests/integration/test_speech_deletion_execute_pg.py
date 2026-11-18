"""Real PostgreSQL deletion execution: one transaction, a durable ledger, replay.

The unit tests pin the decisions with a fake transaction; this module pins the
statements. It seeds real rows through the migrations, previews a plan through the
HTTP route, executes the confirmed scope through the same route, and compares the
answer with what the database actually stores:

* the asset path expires the pinned row, queues the existing content-deletion
  ledger and completes it only after the object version is purged,
* the operation path deletes the authoritative ``speech_job_intents`` row and
  tombstones the legacy private command copy (#540),
* one key maps to one durable row whose replay is byte-identical, including after
  a restart,
* a scope that moved between preview and execution is refused, never partially
  applied, and evidence the deletion must not touch stays intact.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.contract.test_architecture_repairs import _deletion_validator
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.job_runtime import _verify_privileges
from speechnexus_backend.speech.application.deletions import SpeechDeletions
from speechnexus_backend.speech.domain.assets import AssetError, VerifiedObject
from speechnexus_backend.speech.domain.deletion import (
    DeletionPlan,
    DeletionTarget,
    plan_digest,
)
from speechnexus_backend.speech.infrastructure.deletion_executions_postgres import (
    PostgresDeletionExecutions,
)
from speechnexus_backend.speech.infrastructure.deletion_plans_postgres import (
    PostgresDeletionPlans,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_reconciliation_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    deletion_execution_grants,
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
ASSET = "asset-exec-pg"
ASSET_REF = "asset-exec-ref"
ASSET_WRITING = "asset-exec-writing"
JOB = "job-exec-free"
JOB_DRAFT = "job-exec-draft"
DRAFT = "draft-exec-pg"
ARTIFACT = "artifact-exec-pg"
ASSET_KEY = "private/assets/asset-exec-pg/0123456789abcdef0123456789abcdef"
ASSET_REF_KEY = "private/assets/asset-exec-ref/0123456789abcdef0123456789abcdef"
ASSET_WRITING_KEY = "private/assets/asset-exec-writing/0123456789abcdef0123456789abcdef"
NOW_US = 1_760_000_000_000_000
DAY_US = 86_400_000_000
ROLE_PASSWORD = "synthetic_deletion_exec_test"


def sha(letter: str) -> str:
    return "sha256:" + letter * 64


@dataclass(frozen=True)
class Sandbox:
    url: str
    schema: str


class FakeObjects:
    """The object-store identity; records the exact version it was asked to purge."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.versions: list[str] = []
        self.failures = 0
        self.fail_first = fail_first

    def delete_version(self, proof: VerifiedObject) -> None:
        if self.fail_first:
            # A failed purge must leave the ledger queued, never claim success.
            self.fail_first = False
            self.failures += 1
            raise AssetError("asset_object_storage_unavailable")
        self.versions.append(proof.version_id)


def _admin(url: str) -> psycopg.Connection[dict[str, Any]]:
    connection = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    connection.execute("SET TIME ZONE 'UTC'")
    return connection


def _read(sandbox: Sandbox, statement: str, args: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(sandbox.url, row_factory=dict_row) as connection:
        connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        connection.execute("SET TIME ZONE 'UTC'")
        return connection.execute(statement, args).fetchall()


def _seed(admin: psycopg.Connection[dict[str, Any]]) -> None:
    admin.execute(
        """INSERT INTO speech_assets (asset_id, workspace_id, purpose, asset_state,
            upload_phase, bytes, sha256, request_digest, object_key, media_type,
            duration_ms, sample_rate_hz, channels, created_at_us, expires_at_us, writer_id,
            version)
        VALUES (%s, %s, 'voice_reference', 'ready', 'settled', 1024, %s, %s,
                %s, 'audio/wav', 1200, 24000, 1, %s, %s, 'writer-1', 3)""",
        (ASSET, WORKSPACE, sha("a"), sha("b"), ASSET_KEY, NOW_US, NOW_US + DAY_US),
    )
    admin.execute(
        """INSERT INTO speech_asset_objects (asset_id, object_key, storage_version_id,
            bytes, sha256, writer_id, verified_at_us)
        VALUES (%s, %s, 'minio-asset-exec-1', 1024, %s, 'writer-1', %s)""",
        (ASSET, ASSET_KEY, sha("a"), NOW_US),
    )
    admin.execute(
        """INSERT INTO speech_asset_events (event_id, asset_id, event_kind, asset_version,
            occurred_at_us)
        VALUES ('evt-exec-1', %s, 'published', 3, %s)""",
        (ASSET, NOW_US),
    )
    admin.execute(
        """INSERT INTO speech_assets (asset_id, workspace_id, purpose, asset_state,
            upload_phase, bytes, sha256, request_digest, object_key, media_type,
            duration_ms, sample_rate_hz, channels, created_at_us, expires_at_us, writer_id,
            version)
        VALUES (%s, %s, 'voice_reference', 'ready', 'settled', 512, %s, %s,
                %s, 'audio/wav', 900, 24000, 1, %s, %s, 'writer-1', 1)""",
        (ASSET_REF, WORKSPACE, sha("c"), sha("d"), ASSET_REF_KEY, NOW_US, NOW_US + DAY_US),
    )
    admin.execute(
        """INSERT INTO speech_asset_references (reference_id, asset_id, consumer_kind,
            consumer_id, consumer_revision, created_at_us)
        VALUES ('ref-exec-pg', %s, 'voice_revision', 'voice-exec-pg', '2', %s)""",
        (ASSET_REF, NOW_US),
    )
    admin.execute(
        """INSERT INTO speech_assets (asset_id, workspace_id, purpose, asset_state,
            upload_phase, bytes, sha256, request_digest, object_key, media_type,
            duration_ms, sample_rate_hz, channels, created_at_us, expires_at_us, writer_id,
            version)
        VALUES (%s, %s, 'voice_reference', 'staging', 'writing', 256, %s, %s,
                %s, 'audio/wav', 700, 24000, 1, %s, %s, 'writer-1', 1)""",
        (ASSET_WRITING, WORKSPACE, sha("e"), sha("f"), ASSET_WRITING_KEY, NOW_US, NOW_US + DAY_US),
    )
    for job_id, state, status_version, digest in (
        (JOB, "succeeded", 4, sha("1")),
        (JOB_DRAFT, "succeeded", 5, sha("2")),
    ):
        admin.execute(
            """INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id,
                request_digest, state, status_version, result_json, created_at_us, updated_at_us)
            VALUES (%s, %s, 'tts', 'tts-synthetic', %s, %s, %s, '{"synthetic":true}', %s, %s)""",
            (job_id, WORKSPACE, digest, state, status_version, NOW_US, NOW_US),
        )
        admin.execute(
            """INSERT INTO speech_job_intents (job_id, request_digest, request_document,
                recorded_at_us)
            VALUES (%s, %s, '{"input":"synthetic"}', %s)""",
            (job_id, digest, NOW_US),
        )
        # The legacy 0007 private command copy: same operation, same body. The
        # deletion tombstones it because #540 retry would otherwise replay it.
        admin.execute(
            """INSERT INTO admission_operations (operation_id, workspace_id,
                request_digest, request_document, initial_attempt_id, current_attempt_id,
                recorded_at_us)
            VALUES (%s, %s, %s, '{"input":"synthetic"}', 'attempt-1', 'attempt-1', %s)""",
            (job_id, WORKSPACE, digest, NOW_US),
        )
        # Execution evidence that must survive: state transitions, the lease, and
        # the fencing identity of the attempt that produced the content.
        admin.execute(
            """INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us)
            VALUES (%s, 1, 'pending', %s), (%s, 2, 'succeeded', %s)""",
            (job_id, NOW_US, job_id, NOW_US + 1),
        )
    admin.execute(
        """INSERT INTO speech_voice_drafts (draft_id, generation_job_id, kind, status,
            head_revision, metadata_revision, display_name, input_json, binding_json,
            created_at_us, updated_at_us)
        VALUES (%s, %s, 'design', 'draft', '1', 1, 'Synthetic voice',
                '{"text":"synthetic"}', '{"model":"tts-synthetic"}', %s, %s)""",
        (DRAFT, JOB_DRAFT, NOW_US, NOW_US),
    )
    admin.execute(
        """INSERT INTO speech_voice_draft_revisions (draft_id, revision, kind,
            generation_job_id, input_json, binding_json, created_at_us)
        VALUES (%s, '1', 'design', %s, '{"text":"synthetic"}', '{"model":"tts-synthetic"}', %s)""",
        (DRAFT, JOB_DRAFT, NOW_US),
    )
    # An output of the deleted operation. It is a separate resource with its own
    # plan; this deletion neither purges it nor reports it as deleted.
    admin.execute(
        """INSERT INTO speech_artifacts (artifact_id, workspace_id, artifact_kind,
            artifact_state, media_type, bytes, sha256, source_operation_id, object_key,
            storage_version_id, created_at_us, expires_at_us)
        VALUES (%s, %s, 'audio', 'ready', 'audio/wav', 2048, %s, %s,
                'artifacts/artifact-exec-pg.wav', 'minio-artifact-exec-1', %s, %s)""",
        (ARTIFACT, WORKSPACE, sha("9"), JOB, NOW_US, NOW_US + DAY_US),
    )


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not evidence")
    schema = "sn_delexec_" + uuid.uuid4().hex[:10]
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
    admin = _admin(url)
    try:
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        _seed(admin)
        yield Sandbox(url, schema)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _app(
    sandbox: Sandbox, objects: FakeObjects | None = None, pool: Any = None
) -> tuple[Any, Any, FakeObjects]:
    if pool is None:
        pool = speech_pool(sandbox.url, schema=sandbox.schema)
        pool.open()
    objects = objects or FakeObjects()
    service = SpeechDeletions(
        PostgresDeletionPlans(pool),
        workspace_id=WORKSPACE,
        executions=PostgresDeletionExecutions(pool),
        purge=objects,
    )
    return create_app(settings=_settings(), deletions=service), pool, objects


def _body(kind: str, identifier: str, revision: int) -> dict[str, Any]:
    return {
        "target": {
            "owner": "speech",
            "kind": kind,
            "resource": {"id": identifier, "revision": str(revision)},
        },
        "mode": "ordinary",
    }


async def _post(app: Any, path: str, body: dict[str, Any], key: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        return await http.post(path, json=body, headers={"Idempotency-Key": key})


async def _preview(app: Any, kind: str, identifier: str, revision: int, key: str) -> dict[str, Any]:
    response = await _post(app, "/v1/nexus/deletion-plans", _body(kind, identifier, revision), key)
    assert response.status_code == 200, response.text
    payload = response.json()
    _deletion_validator("DeletionPlan").validate(payload)
    return cast(dict[str, Any], payload)


def _confirm(plan: dict[str, Any], targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "plan_id": plan["id"],
        "expected_plan_revision": plan["plan_revision"],
        "plan_digest": plan["plan_digest"],
        "confirm_targets": targets if targets is not None else [plan["target"]],
    }


async def _execute(app: Any, body: dict[str, Any], key: str) -> httpx.Response:
    return await _post(app, "/v1/nexus/deletions", body, key)


def _ledger(sandbox: Sandbox, key: str) -> list[dict[str, Any]]:
    from speechnexus_backend.speech.domain.deletion import idempotency_key_digest

    return _read(
        sandbox,
        "SELECT * FROM speech_deletion_ledger WHERE workspace_id = %s "
        "AND idempotency_key_digest = %s",
        (WORKSPACE, idempotency_key_digest(key)),
    )


async def test_asset_execution_purges_the_pinned_version_and_records_the_ledger(
    sandbox: Sandbox,
) -> None:
    """Deletes the object version the plan pinned; the ledger says exactly that."""
    app, pool, objects = _app(sandbox)
    try:
        plan = await _preview(app, "asset", ASSET, 3, "key-plan-asset")
        response = await _execute(app, _confirm(plan), "key-exec-asset")
        assert response.status_code == 202, response.text
        payload = response.json()
        _deletion_validator("Deletion").validate(payload)

        # The answer is a completed deletion of exactly the pinned target.
        assert payload["status"] == "succeeded"
        assert payload["cleanup_state"] == "complete"
        assert payload["error"] is None
        assert payload["plan_id"] == plan["id"]
        assert payload["plan_digest"] == plan["plan_digest"]
        assert payload["result"]["deleted"] == [plan["target"]]
        assert payload["result"]["retained_independent_copies"] == []
        assert payload["result"]["ledger_id"] == payload["id"]
        # Only the pinned object version was purged, exactly once.
        assert objects.versions == ["minio-asset-exec-1"]

        row = _ledger(sandbox, "key-exec-asset")
        assert len(row) == 1
        assert row[0]["status"] == "succeeded"
        assert row[0]["cleanup_state"] == "complete"
        assert row[0]["plan_id"] == plan["id"]
        assert row[0]["plan_revision"] == plan["plan_revision"]
        assert row[0]["target_kind"] == "asset"
        assert row[0]["target_id"] == ASSET
        assert row[0]["deleted"] == [["speech", "asset", ASSET, "3"]]
        assert row[0]["retained"] == []
        assert row[0]["completed_at"] is not None

        asset = _read(
            sandbox, "SELECT asset_state, version FROM speech_assets WHERE asset_id = %s", (ASSET,)
        )
        assert asset == [{"asset_state": "deleted", "version": 5}]
        content = _read(
            sandbox,
            "SELECT asset_id, reason, deletion_state, storage_version_id, completed_at_us "
            "FROM speech_content_deletions",
        )
        assert len(content) == 1
        assert content[0]["asset_id"] == ASSET
        assert content[0]["reason"] == "confirmed_deletion"
        assert content[0]["deletion_state"] == "complete"
        assert content[0]["storage_version_id"] == "minio-asset-exec-1"
        assert content[0]["completed_at_us"] is not None
        kinds = _read(
            sandbox,
            "SELECT event_kind FROM speech_asset_events WHERE asset_id = %s ORDER BY event_kind",
            (ASSET,),
        )
        # The pre-existing evidence row survives; the deletion appends its own.
        assert kinds == [
            {"event_kind": "expired"},
            {"event_kind": "object_deleted"},
            {"event_kind": "published"},
        ]
    finally:
        pool.close()


async def test_the_same_key_returns_the_same_row_and_never_purges_twice(sandbox: Sandbox) -> None:
    app, pool, objects = _app(sandbox)
    try:
        plan = await _preview(app, "asset", ASSET, 3, "key-plan-idem")
        body = _confirm(plan)
        first = await _execute(app, body, "key-exec-idem")
        assert first.status_code == 202, first.text
        second = await _execute(app, body, "key-exec-idem")
        assert second.status_code == 202, second.text
        # Byte-identical: same id, ledger id and completion instant.
        assert second.json() == first.json()
        assert len(_ledger(sandbox, "key-exec-idem")) == 1
        assert objects.versions == ["minio-asset-exec-1"]
        # A different, individually valid intent under the same key is a conflict,
        # not a replay: the key names one request, not one request shape.
        other = await _preview(app, "operation_content", JOB, 4, "key-plan-idem-other")
        conflict = await _execute(app, _confirm(other), "key-exec-idem")
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        assert len(_ledger(sandbox, "key-exec-idem")) == 1
        assert objects.versions == ["minio-asset-exec-1"]
        # Nothing of the other plan was deleted by the refused request.
        assert _read(
            sandbox, "SELECT count(*) AS n FROM speech_job_intents WHERE job_id = %s", (JOB,)
        ) == [{"n": 1}]
    finally:
        pool.close()


async def test_a_moved_scope_is_refused_without_touching_content(sandbox: Sandbox) -> None:
    app, pool, objects = _app(sandbox)
    try:
        plan = await _preview(app, "asset", ASSET, 3, "key-plan-moved")
        # A writer bumps the asset between preview and execution: the plan's pinned
        # revision is no longer the row's, so this is a different deletion.
        admin = _admin(sandbox.url)
        try:
            admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            admin.execute("UPDATE speech_assets SET version = 4 WHERE asset_id = %s", (ASSET,))
        finally:
            admin.close()
        response = await _execute(app, _confirm(plan), "key-exec-moved")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "deletion_plan_changed"
        assert _ledger(sandbox, "key-exec-moved") == []
        assert objects.versions == []
        assert _read(
            sandbox, "SELECT asset_state FROM speech_assets WHERE asset_id = %s", (ASSET,)
        ) == [{"asset_state": "ready"}]
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_content_deletions") == [{"n": 0}]
    finally:
        pool.close()


async def test_a_tampered_plan_digest_is_refused_with_409(sandbox: Sandbox) -> None:
    """The digest is recomputed, never trusted: it identifies the exact plan."""
    app, pool, objects = _app(sandbox)
    try:
        plan = await _preview(app, "asset", ASSET, 3, "key-plan-digest")
        body = _confirm(plan)
        body["plan_digest"] = "sha256:" + "0" * 64
        response = await _execute(app, body, "key-exec-digest")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "deletion_plan_changed"
        assert _ledger(sandbox, "key-exec-digest") == []
        assert objects.versions == []
        # The untampered plan is still usable under its own key.
        accepted = await _execute(app, _confirm(plan), "key-exec-digest-ok")
        assert accepted.status_code == 202, accepted.text
        assert objects.versions == ["minio-asset-exec-1"]
    finally:
        pool.close()


async def test_a_confirmation_must_be_set_equal_to_the_plan_scope(sandbox: Sandbox) -> None:
    app, pool, objects = _app(sandbox)
    try:
        plan = await _preview(app, "asset", ASSET, 3, "key-plan-set")
        extra = {
            "owner": "speech",
            "kind": "artifact",
            "resource": {"id": ARTIFACT, "revision": "1"},
        }
        # Missing, extra and duplicate confirmations are all invalid requests; a
        # target outside the plan's eligible set is a scope mismatch.
        for body, code in (
            (_confirm(plan, targets=[]), "invalid_deletion_request"),
            (_confirm(plan, targets=[plan["target"], extra]), "deletion_confirmation_mismatch"),
            (_confirm(plan, targets=[plan["target"], plan["target"]]), "invalid_deletion_request"),
        ):
            response = await _execute(app, body, "key-exec-set")
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == code
        assert _ledger(sandbox, "key-exec-set") == []
        assert objects.versions == []
    finally:
        pool.close()


async def test_an_expired_plan_is_refused_with_409(sandbox: Sandbox) -> None:
    """An issued plan has a fixed 300s window; past it the plan is not a credential.

    The row cannot simply be backdated: ``plan_digest`` covers ``expires_at`` and
    the table's CHECK keeps the window fixed, so the fixture issues a real plan
    whose window already closed, exactly as a six-minute-old plan would look.
    """
    app, pool, objects = _app(sandbox)
    try:
        preview = await _preview(app, "asset", ASSET, 3, "key-plan-expired")
        stored = _read(
            sandbox,
            "SELECT content_snapshot FROM speech_deletion_plans WHERE id = %s",
            (preview["id"],),
        )[0]["content_snapshot"]
        now = datetime.now(UTC)
        issued = DeletionPlan(
            id="dp-exec-expired",
            workspace_id=WORKSPACE,
            target=DeletionTarget("speech", "asset", ASSET, 3),
            mode="ordinary",
            revision=1,
            created_at=now - timedelta(seconds=400),
            expires_at=now - timedelta(seconds=100),
            dependencies=(),
        )
        digest = plan_digest(issued)
        assert issued.expires_at < now
        admin = _admin(sandbox.url)
        try:
            admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            admin.execute(
                "INSERT INTO speech_deletion_plans (id, workspace_id, target_owner, "
                "target_kind, target_id, target_revision, target_digest, mode, plan_revision, "
                "plan_digest, idempotency_key_digest, intent_digest, created_at, expires_at, "
                "dependencies, content_snapshot) VALUES (%s, %s, 'speech', 'asset', %s, '3', "
                "%s, 'ordinary', '1', %s, %s, %s, %s, %s, '[]'::jsonb, %s)",
                (
                    issued.id,
                    WORKSPACE,
                    ASSET,
                    sha("a"),
                    digest,
                    "a" * 64,
                    "b" * 64,
                    issued.created_at,
                    issued.expires_at,
                    Jsonb(stored),
                ),
            )
        finally:
            admin.close()
        request = {
            "plan_id": issued.id,
            "expected_plan_revision": "1",
            "plan_digest": digest,
            "confirm_targets": [preview["target"]],
        }
        response = await _execute(app, request, "key-exec-expired")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "deletion_plan_expired"
        assert _ledger(sandbox, "key-exec-expired") == []
        assert objects.versions == []
        assert _read(
            sandbox, "SELECT asset_state FROM speech_assets WHERE asset_id = %s", (ASSET,)
        ) == [{"asset_state": "ready"}]
    finally:
        pool.close()


async def test_an_ineligible_plan_and_a_second_key_are_both_refused(sandbox: Sandbox) -> None:
    app, pool, objects = _app(sandbox)
    try:
        blocked = await _preview(app, "asset", ASSET_REF, 1, "key-plan-blocked")
        assert blocked["eligible"] is False
        response = await _execute(app, _confirm(blocked), "key-exec-blocked")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "deletion_dependency_conflict"

        # An issued plan is a single-use credential: a second key cannot spend it.
        plan = await _preview(app, "asset", ASSET, 3, "key-plan-once")
        first = await _execute(app, _confirm(plan), "key-exec-once-a")
        assert first.status_code == 202, first.text
        second = await _execute(app, _confirm(plan), "key-exec-once-b")
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "deletion_plan_changed"
        assert len(_read(sandbox, "SELECT id FROM speech_deletion_ledger")) == 1
        assert objects.versions == ["minio-asset-exec-1"]
    finally:
        pool.close()


async def test_a_failed_purge_stays_queued_and_the_same_key_resumes(sandbox: Sandbox) -> None:
    """Success is never claimed without a confirmed purge; the row is resumable."""
    app, pool, objects = _app(sandbox, FakeObjects(fail_first=True))
    try:
        plan = await _preview(app, "asset", ASSET, 3, "key-plan-resume")
        first = await _execute(app, _confirm(plan), "key-exec-resume")
        assert first.status_code == 202, first.text
        queued = first.json()
        _deletion_validator("Deletion").validate(queued)
        assert queued["status"] == "queued"
        assert queued["cleanup_state"] == "pending"
        assert queued["result"] is None
        assert objects.versions == []
        assert objects.failures == 1
        row = _ledger(sandbox, "key-exec-resume")
        assert len(row) == 1 and row[0]["status"] == "queued" and row[0]["completed_at"] is None

        resumed = await _execute(app, _confirm(plan), "key-exec-resume")
        assert resumed.status_code == 202, resumed.text
        done = resumed.json()
        assert done["status"] == "succeeded"
        assert done["cleanup_state"] == "complete"
        assert done["id"] == queued["id"]
        assert done["result"]["ledger_id"] == queued["id"]
        assert objects.versions == ["minio-asset-exec-1"]
        assert len(_ledger(sandbox, "key-exec-resume")) == 1
    finally:
        pool.close()


async def test_a_restart_replays_the_stored_result(sandbox: Sandbox) -> None:
    """A new process, pool and service still answer the same key from the ledger."""
    app, pool, objects = _app(sandbox)
    plan = await _preview(app, "asset", ASSET, 3, "key-plan-restart")
    first = await _execute(app, _confirm(plan), "key-exec-restart")
    assert first.status_code == 202, first.text
    pool.close()

    restarted_app, restarted_pool, restarted_objects = _app(sandbox)
    try:
        replay = await _execute(restarted_app, _confirm(plan), "key-exec-restart")
        assert replay.status_code == 202, replay.text
        assert replay.json() == first.json()
        # Nothing was deleted twice: the fresh process purged nothing.
        assert restarted_objects.versions == []
        assert objects.versions == ["minio-asset-exec-1"]
        assert len(_ledger(sandbox, "key-exec-restart")) == 1
    finally:
        restarted_pool.close()


async def test_operation_content_deletes_the_intent_and_keeps_the_evidence(
    sandbox: Sandbox,
) -> None:
    """#540: the public intent row goes, the legacy command copy is tombstoned.

    Deleted: the operation's public intent (document + digest) and the private
    command body of the same operation. Retained: the operation row itself, its
    state events, the admission lease and fencing identity, and the output
    artifact (a separate resource with its own plan).
    """
    app, pool, objects = _app(sandbox)
    try:
        plan = await _preview(app, "operation_content", JOB, 4, "key-plan-job")
        assert plan["eligible"] is True
        response = await _execute(app, _confirm(plan), "key-exec-job")
        assert response.status_code == 202, response.text
        payload = response.json()
        _deletion_validator("Deletion").validate(payload)
        assert payload["status"] == "succeeded"
        assert payload["cleanup_state"] == "complete"
        assert payload["result"]["deleted"] == [plan["target"]]
        assert payload["result"]["retained_independent_copies"] == []
        # A job target has no object to purge.
        assert objects.versions == []

        deleted = _read(
            sandbox, "SELECT count(*) AS n FROM speech_job_intents WHERE job_id = %s", (JOB,)
        )
        assert deleted == [{"n": 0}]
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_job_intents") == [{"n": 1}]
        command = _read(
            sandbox,
            "SELECT request_document, command_redacted_at_us, command_deletion_job_id, "
            "command_deletion_evidence_digest, version FROM admission_operations "
            "WHERE operation_id = %s",
            (JOB,),
        )
        assert len(command) == 1
        assert command[0]["request_document"] is None
        assert command[0]["command_redacted_at_us"] is not None
        assert command[0]["command_deletion_job_id"] == payload["id"]
        assert command[0]["command_deletion_evidence_digest"] == plan["plan_digest"]
        assert command[0]["version"] == 2
        # The untouched operation's command body is still present.
        other = _read(
            sandbox,
            "SELECT request_document FROM admission_operations WHERE operation_id = %s",
            (JOB_DRAFT,),
        )
        assert other[0]["request_document"] == '{"input":"synthetic"}'

        # Execution, metering and fencing evidence survives.
        for table, expected in (
            ("speech_job_events", 4),
            ("speech_jobs", 2),
            # No lease or attempt exists in this fixture, and the deletion creates
            # none: execution, metering and fencing evidence is never a target.
            ("admission_leases", 0),
            ("admission_attempts", 0),
            ("speech_artifacts", 1),
            ("speech_voice_draft_revisions", 1),
        ):
            assert _read(sandbox, f"SELECT count(*) AS n FROM {table}") == [{"n": expected}]
        ledger = _ledger(sandbox, "key-exec-job")
        assert ledger[0]["deleted"] == [["speech", "operation_content", JOB, "4"]]
        assert ledger[0]["retained"] == []
        assert ledger[0]["content_ledger_id"] is None
    finally:
        pool.close()


async def test_a_writing_asset_is_refused_at_execution_time(sandbox: Sandbox) -> None:
    app, pool, objects = _app(sandbox)
    try:
        plan = await _preview(app, "asset", ASSET_WRITING, 1, "key-plan-writing")
        response = await _execute(app, _confirm(plan), "key-exec-writing")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "deletion_dependency_conflict"
        assert _ledger(sandbox, "key-exec-writing") == []
        assert objects.versions == []
    finally:
        pool.close()


async def test_execution_and_the_asset_writer_do_not_deadlock(sandbox: Sandbox) -> None:
    """Workspace first, then asset: the same order every asset writer takes."""
    app, pool, objects = _app(sandbox)
    plan = await _preview(app, "asset", ASSET, 3, "key-plan-race")
    started, published = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def writer() -> None:
        try:
            with psycopg.connect(sandbox.url, row_factory=dict_row) as connection:
                connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
                connection.execute(
                    "SELECT 1 FROM speech_workspaces WHERE workspace_id = %s FOR UPDATE",
                    (WORKSPACE,),
                )
                started.set()
                time.sleep(0.5)
                connection.execute(
                    "SELECT 1 FROM speech_assets WHERE asset_id = %s FOR UPDATE", (ASSET,)
                )
                connection.commit()
                published.set()
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert started.wait(5)
        response = await asyncio.to_thread(
            asyncio.run, _execute(app, _confirm(plan), "key-exec-race")
        )
        thread.join(10)
        assert failures == []
        assert published.is_set()
        assert response.status_code == 202, response.text
        assert objects.versions == ["minio-asset-exec-1"]
    finally:
        thread.join(10)
        pool.close()


def _provision_writer(sandbox: Sandbox, *, include_execution: bool) -> tuple[str, Any]:
    role = "sn_delexec_" + uuid.uuid4().hex[:10]
    admin = _admin(sandbox.url)
    admin.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(ROLE_PASSWORD)
        )
    )
    admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
    for grant in (
        f'GRANT USAGE ON SCHEMA "{sandbox.schema}" TO "{role}"',
        *job_runtime_grants(sandbox.schema, role),
        *voice_draft_grants(sandbox.schema, role),
        *public_job_grants(sandbox.schema, role),
        *job_dispatch_grants(sandbox.schema, role),
        *cancellation_reconciliation_grants(sandbox.schema, role),
        *public_job_submission_grants(sandbox.schema, role),
        *deletion_plan_grants(sandbox.schema, role),
        *(deletion_execution_grants(sandbox.schema, role) if include_execution else ()),
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


async def test_the_provisioned_writer_role_can_execute_both_targets(sandbox: Sandbox) -> None:
    """The grant surface is proven by the identity the deployment actually uses."""
    role, admin = _provision_writer(sandbox, include_execution=True)
    pool = None
    try:
        with psycopg.connect(
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
        app, _, objects = _app(sandbox, pool=pool)
        asset_plan = await _preview(app, "asset", ASSET, 3, "key-role-plan-asset")
        asset = await _execute(app, _confirm(asset_plan), "key-role-exec-asset")
        assert asset.status_code == 202, asset.text
        assert asset.json()["status"] == "succeeded"
        assert objects.versions == ["minio-asset-exec-1"]
        # The writer identity may DELETE only the confirmable intent relation.
        assert _read(
            sandbox, "SELECT count(*) AS n FROM speech_job_intents WHERE job_id = %s", (JOB,)
        ) == [{"n": 1}]
        job_plan = await _preview(app, "operation_content", JOB, 4, "key-role-plan-job")
        job = await _execute(app, _confirm(job_plan), "key-role-exec-job")
        assert job.status_code == 202, job.text
        assert job.json()["status"] == "succeeded"
        assert _read(
            sandbox, "SELECT count(*) AS n FROM speech_job_intents WHERE job_id = %s", (JOB,)
        ) == [{"n": 0}]
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


async def test_missing_execution_grants_fail_the_route_not_the_process(sandbox: Sandbox) -> None:
    """A writer without the execution grants boots; the route names the gap."""
    role, admin = _provision_writer(sandbox, include_execution=False)
    pool = None
    try:
        with psycopg.connect(
            make_conninfo(sandbox.url, user=role, password=ROLE_PASSWORD), row_factory=dict_row
        ) as connection:
            connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            _verify_privileges(connection, sandbox.schema)
        pool = speech_pool(
            make_conninfo(sandbox.url, user=role, password=ROLE_PASSWORD),
            schema=sandbox.schema,
        )
        pool.open()
        app, _, objects = _app(sandbox, pool=pool)
        plan = await _preview(app, "asset", ASSET, 3, "key-nogrant-plan")
        response = await _execute(app, _confirm(plan), "key-nogrant-exec")
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["code"] == "deletion_not_configured"
        assert error["retryable"] is False
        assert "GRANT" in error["message"] or "apply" in error["message"]
        assert objects.versions == []
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_deletion_ledger") == [{"n": 0}]
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()
