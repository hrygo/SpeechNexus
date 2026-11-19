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
import hashlib
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
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
from speechnexus_backend.speech.domain.artifacts import ArtifactError
from speechnexus_backend.speech.domain.assets import AssetError, VerifiedObject
from speechnexus_backend.speech.domain.deletion import (
    DeletionIdempotencyConflictError,
    DeletionPlan,
    DeletionRequest,
    DeletionTarget,
    confirmed_targets,
    deletion_intent_digest,
    plan_digest,
)
from speechnexus_backend.speech.infrastructure.deletion_executions_postgres import (
    PostgresDeletionExecutions,
    _DeletionExecution,
    _Transaction,
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
REV2_JOB = "job-exec-rev2"
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
        self.on_delete: Callable[[VerifiedObject], None] | None = None

    def delete_version(self, proof: VerifiedObject) -> None:
        if self.on_delete is not None:
            self.on_delete(proof)
        if self.fail_first:
            # A failed purge must leave the ledger queued, never claim success.
            self.fail_first = False
            self.failures += 1
            raise AssetError("asset_object_storage_unavailable")
        self.versions.append(proof.version_id)


class FakeArtifactPurge:
    """The Artifact bucket identity; records the pinned object version it purged."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.pinned: list[tuple[str, str]] = []
        self.fail_first = fail_first

    def delete_pinned(self, object_key: str, version_id: str) -> None:
        if self.fail_first:
            self.fail_first = False
            raise ArtifactError("artifact_cleanup_unconfirmed")
        self.pinned.append((object_key, version_id))


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
            VALUES (%s, %s, %s, '{"input":"synthetic"}', 'attempt-exec', 'attempt-exec', %s)""",
            (job_id, WORKSPACE, digest, NOW_US),
        )
        # Execution evidence that must survive: state transitions, the lease, and
        # the fencing identity of the attempt that produced the content.
        admin.execute(
            """INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us)
            VALUES (%s, 1, 'pending', %s), (%s, 2, 'succeeded', %s)""",
            (job_id, NOW_US, job_id, NOW_US + 1),
        )
    # A real reserved lease for the deleted operation: capacity, attempt identity,
    # instance and metering facts that deletion must never touch.
    admin.execute(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES ('tts-realtime', 'realtime', 'active')"
    )
    admin.execute(
        "INSERT INTO workspace_quotas (workspace_id, max_attempts, held_attempts, "
        "policy_revision) VALUES (%s, 8, 1, 'policy-exec')",
        (WORKSPACE,),
    )
    admin.execute(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, topology_digest, "
        "allocatable_unified_physical_memory_bytes, held_unified_physical_memory_bytes) "
        "VALUES ('host-exec', 'unified_memory', 'active', %s, 128, 64)",
        (sha("7"),),
    )
    admin.execute(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id, "
        "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
        "loading_policy, config_digest, instance_state, max_slots, held_slots) VALUES "
        "('instance-exec', 1, 'host-exec', 'tts-realtime', 'gateway-exec', 'binding-exec', "
        "'manifest-exec', 'template-exec', 'managed', 'on_demand', %s, 'ready', 1, 1)",
        (sha("8"),),
    )
    admin.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
        "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
        "request_digest, required_stage_ids) VALUES (%s, 'attempt-exec', %s, 'binding-exec', "
        "'manifest-exec', %s, 'policy-exec', 'realtime', %s, %s)",
        (JOB, WORKSPACE, sha("6"), sha("5"), '["lease-exec"]'),
    )
    admin.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
        "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
        "reserved_slots, deadline_at_us, reserved_unified_physical_memory_bytes) VALUES "
        "('lease-exec', %s, 'attempt-exec', 'lease-exec', 'host-exec', 'instance-exec', 1, "
        "'tts-realtime', 'gateway-exec', 7, 1, 1, 32)",
        (JOB,),
    )
    admin.execute(
        "INSERT INTO speech_tts_settlement (lease_id, operation_id, attempt_id, "
        "submit_intent_id, target_document, deadline_at_us, next_check_at_us, settled_at_us) "
        "VALUES ('lease-exec', %s, 'attempt-exec', 'submit-exec-1', '{\"meter\":1}', 1, 0, 1)",
        (JOB,),
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
    sandbox: Sandbox,
    objects: FakeObjects | None = None,
    pool: Any = None,
    artifact_objects: FakeArtifactPurge | None = None,
) -> tuple[Any, Any, FakeObjects, FakeArtifactPurge]:
    if pool is None:
        pool = speech_pool(sandbox.url, schema=sandbox.schema)
        pool.open()
    objects = objects or FakeObjects()
    artifact_objects = artifact_objects or FakeArtifactPurge()
    service = SpeechDeletions(
        PostgresDeletionPlans(pool),
        workspace_id=WORKSPACE,
        executions=PostgresDeletionExecutions(pool),
        purge=objects,
        artifact_purge=artifact_objects,
    )
    return create_app(settings=_settings(), deletions=service), pool, objects, artifact_objects


def _seed_unreferenced_revision(sandbox: Sandbox, revision: str = "2") -> None:
    """One draft revision that is not the head and nothing references yet."""
    with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        admin.execute("SET TIME ZONE 'UTC'")
        admin.execute(
            """INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id,
                request_digest, state, status_version, result_json, created_at_us, updated_at_us)
            VALUES (%s, %s, 'tts', 'tts-synthetic', %s, 'succeeded', 2,
                    '{"synthetic":true}', %s, %s)""",
            (REV2_JOB, WORKSPACE, sha("3"), NOW_US, NOW_US),
        )
        admin.execute(
            """INSERT INTO speech_voice_draft_revisions (draft_id, revision, kind,
                generation_job_id, input_json, binding_json, created_at_us)
            VALUES (%s, %s, 'design', %s, '{"text":"synthetic two"}',
                    '{"model":"tts-synthetic"}', %s)""",
            (DRAFT, revision, REV2_JOB, NOW_US),
        )


def _seed_job_intent_referencing(sandbox: Sandbox, revision: str = "2") -> None:
    """A retained public Job intent naming the draft revision (#540 surface)."""
    document = (
        '{"model":"tts-synthetic","input":{"voice_draft_id":"' + DRAFT + '",'
        '"draft_revision":"' + revision + '"}}'
    )
    with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        admin.execute(
            """INSERT INTO speech_job_intents (job_id, request_digest, request_document,
                recorded_at_us) VALUES (%s, %s, %s, %s)""",
            (REV2_JOB, sha("4"), document, NOW_US),
        )


def _seed_evidence_reporting_the_artifact(sandbox: Sandbox) -> None:
    """Real execution evidence whose report Artifact is the deletion target."""
    with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        admin.execute(
            """INSERT INTO speech_voice_capability_evidence (
                evidence_id, draft_id, draft_revision, validation_job_id, validation_id,
                binding_revision_id, report_artifact_id, report_sha256, report_receipt_id,
                evidence_kind, measured_by, measured_at_us, capabilities, coverage,
                evidence_digest)
            VALUES (%s, %s, '2', %s, 'validation-exec', 'binding-exec', %s, %s,
                    'receipt-exec', 'validation_run', 'synthetic-evaluator', %s,
                    '{}'::jsonb, '{}'::jsonb, %s)""",
            ("vce-" + REV2_JOB, DRAFT, REV2_JOB, ARTIFACT, sha("9"), NOW_US, sha("5")),
        )


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


def _evidence(sandbox: Sandbox) -> dict[str, list[dict[str, Any]]]:
    """Every fact a content deletion must leave byte-identical."""
    return {
        "leases": _read(sandbox, "SELECT * FROM admission_leases ORDER BY lease_id"),
        "attempts": _read(sandbox, "SELECT * FROM admission_attempts ORDER BY attempt_id"),
        "settlement": _read(sandbox, "SELECT * FROM speech_tts_settlement ORDER BY lease_id"),
        "instances": _read(sandbox, "SELECT * FROM engine_instances ORDER BY instance_id"),
        "quotas": _read(sandbox, "SELECT * FROM workspace_quotas ORDER BY workspace_id"),
        "fencing": _read(
            sandbox,
            "SELECT operation_id, initial_attempt_id, current_attempt_id "
            "FROM admission_operations ORDER BY operation_id",
        ),
    }


def _plan(
    pool: Any, kind: str, identifier: str, revision: int, key: str, mode: str = "ordinary"
) -> DeletionPlan:
    service = SpeechDeletions(PostgresDeletionPlans(pool), workspace_id=WORKSPACE)
    return service.preview(DeletionTarget("speech", kind, identifier, revision), mode, key)


def _confirmation_of(plan: DeletionPlan) -> DeletionRequest:
    return DeletionRequest(plan.id, plan.revision, plan_digest(plan), confirmed_targets(plan))


def _blocked_on_the_key(sandbox: Sandbox) -> bool:
    rows = _read(
        sandbox,
        "SELECT count(*) AS n FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
        "AND strpos(query, 'INSERT INTO speech_deletion_ledger') > 0",
    )
    return bool(rows and rows[0]["n"])


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
    app, pool, objects, _artifacts = _app(sandbox)
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
    app, pool, objects, _artifacts = _app(sandbox)
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
    app, pool, objects, _artifacts = _app(sandbox)
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
    app, pool, objects, _artifacts = _app(sandbox)
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
    app, pool, objects, _artifacts = _app(sandbox)
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
    app, pool, objects, _artifacts = _app(sandbox)
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


async def test_an_ineligible_plan_is_recorded_as_blocked(sandbox: Sandbox) -> None:
    app, pool, objects, _artifacts = _app(sandbox)
    try:
        blocked = await _preview(app, "asset", ASSET_REF, 1, "key-plan-blocked")
        assert blocked["eligible"] is False
        response = await _execute(app, _confirm(blocked), "key-exec-blocked")
        assert response.status_code == 202, response.text
        payload = response.json()
        _deletion_validator("Deletion").validate(payload)
        assert payload["status"] == "blocked"
        assert payload["cleanup_state"] == "pending"
        assert payload["result"] is None
        assert payload["error"]["code"] == "deletion_target_in_use"
        assert payload["error"]["retryable"] is False
        assert "voice-exec-pg@2" in payload["error"]["message"]
        assert objects.versions == []
        assert _read(
            sandbox, "SELECT asset_state FROM speech_assets WHERE asset_id = %s", (ASSET_REF,)
        ) == [{"asset_state": "ready"}]

        # An issued plan is a single-use credential: a second key cannot spend it.
        plan = await _preview(app, "asset", ASSET, 3, "key-plan-once")
        first = await _execute(app, _confirm(plan), "key-exec-once-a")
        assert first.status_code == 202, first.text
        second = await _execute(app, _confirm(plan), "key-exec-once-b")
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "deletion_plan_changed"
        assert len(_read(sandbox, "SELECT id FROM speech_deletion_ledger")) == 2
        assert objects.versions == ["minio-asset-exec-1"]
    finally:
        pool.close()


async def test_a_failed_purge_stays_queued_and_the_same_key_resumes(sandbox: Sandbox) -> None:
    """Success is never claimed without a confirmed purge; the row is resumable."""
    app, pool, objects, _artifacts = _app(sandbox, FakeObjects(fail_first=True))
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
    """A new pool and service in this process still answer the same key."""
    app, pool, objects, _artifacts = _app(sandbox)
    plan = await _preview(app, "asset", ASSET, 3, "key-plan-restart")
    first = await _execute(app, _confirm(plan), "key-exec-restart")
    assert first.status_code == 202, first.text
    pool.close()

    restarted_app, restarted_pool, restarted_objects, _restarted_artifacts = _app(sandbox)
    try:
        replay = await _execute(restarted_app, _confirm(plan), "key-exec-restart")
        assert replay.status_code == 202, replay.text
        assert replay.json() == first.json()
        # Nothing was deleted twice: the restarted service read the stored ledger
        # row and did not touch the object version again.
        assert restarted_objects.versions == []
        assert objects.versions == ["minio-asset-exec-1"]
        assert len(_ledger(sandbox, "key-exec-restart")) == 1
    finally:
        restarted_pool.close()


async def test_a_live_purge_claim_answers_503_until_the_crash_lease_expires(
    sandbox: Sandbox,
) -> None:
    """A crashed executor's claim is a wait, not a second purge.

    Acceptance and the object purge are separate transactions. If the process dies
    after acceptance, the ledger row is ``queued`` with a live ``purge_claimed_at``
    and no completion. A same-key replay inside the 120 s lease waits, then answers
    ``503 deletion_unavailable`` without touching the Artifact. Once the lease has
    expired the same key reclaims the row and finishes the purge.
    """
    app, pool, _objects, artifacts = _app(sandbox)
    try:
        plan = await _preview(app, "artifact", ARTIFACT, 1, "key-plan-crash-lease")
        body = _confirm(plan)
        target = plan["target"]
        request = DeletionRequest(
            plan["id"],
            int(plan["plan_revision"]),
            plan["plan_digest"],
            (
                DeletionTarget(
                    "speech",
                    target["kind"],
                    target["resource"]["id"],
                    int(target["resource"]["revision"]),
                ),
            ),
        )
        key = "key-exec-crash-lease"
        key_digest = hashlib.sha256(key.encode()).hexdigest()
        with pool.connection(timeout=5) as connection:
            transaction = _Transaction(connection, time.monotonic() + 5, WORKSPACE)
            accepted = _DeletionExecution(transaction, WORKSPACE).accept(
                request,
                key_digest=key_digest,
                intent_digest=deletion_intent_digest(request),
                purge_available=True,
                artifact_purge_available=True,
                request_id="crash-lease",
            )
            assert accepted.record.status == "queued"
            connection.commit()
        assert artifacts.pinned == []
        claimed = _read(
            sandbox,
            "SELECT status, purge_claimed_at IS NOT NULL AS live "
            "FROM speech_deletion_ledger WHERE id = %s",
            (accepted.record.id,),
        )
        assert claimed == [{"status": "queued", "live": True}]

        replay = await _execute(app, body, key)
        assert replay.status_code == 503, replay.text
        assert replay.json()["error"]["code"] == "deletion_unavailable"
        assert replay.json()["error"]["retryable"] is True
        assert artifacts.pinned == []
        assert _read(
            sandbox,
            "SELECT count(*) AS n FROM speech_deletion_ledger WHERE id = %s AND status = 'queued'",
            (accepted.record.id,),
        ) == [{"n": 1}]

        with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as admin:
            admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            admin.execute(
                "UPDATE speech_deletion_ledger "
                "SET purge_claimed_at = clock_timestamp() - interval '121 seconds', "
                "created_at = clock_timestamp() - interval '122 seconds' "
                "WHERE id = %s",
                (accepted.record.id,),
            )

        resumed = await _execute(app, body, key)
        assert resumed.status_code == 202, resumed.text
        payload = resumed.json()
        _deletion_validator("Deletion").validate(payload)
        assert payload["status"] == "succeeded"
        assert payload["id"] == accepted.record.id
        assert artifacts.pinned == [("artifacts/artifact-exec-pg.wav", "minio-artifact-exec-1")]
        assert _read(
            sandbox,
            "SELECT count(*) AS n FROM speech_deletion_ledger WHERE id = %s",
            (accepted.record.id,),
        ) == [{"n": 1}]
    finally:
        pool.close()


async def test_operation_content_deletes_the_intent_and_keeps_the_evidence(
    sandbox: Sandbox,
) -> None:
    """#540: the public intent row goes, the legacy command copy is tombstoned.

    Deleted: the operation's public intent (document + digest) and the private
    command body of the same operation. Retained: the operation row itself, its
    state events, the admission lease and fencing identity, and the output
    artifact (a separate resource with its own plan).
    """
    app, pool, objects, _artifacts = _app(sandbox)
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
            # The lease and attempt seeded for this operation survive: execution,
            # metering and fencing evidence is never a target.
            ("admission_leases", 1),
            ("admission_attempts", 1),
            ("speech_tts_settlement", 1),
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


async def test_execution_evidence_and_settlement_are_unchanged(sandbox: Sandbox) -> None:
    """Capacity, attempt identity, fencing and metering are not deletion targets.

    The fixture holds a real reserved lease, its attempt, the instance that served
    it, the workspace quota count and one settlement row, so "unchanged" is a
    real negative: those rows exist before the delete and must still exist, byte
    for byte, afterwards.
    """
    app, pool, objects, _artifacts = _app(sandbox)
    try:
        before = _evidence(sandbox)
        assert before["leases"] and before["attempts"] and before["settlement"]
        plan = await _preview(app, "operation_content", JOB, 4, "key-plan-evidence")
        response = await _execute(app, _confirm(plan), "key-exec-evidence")
        assert response.status_code == 202, response.text
        after = _evidence(sandbox)
        for table in ("leases", "attempts", "settlement", "instances", "quotas"):
            assert after[table] == before[table], table
        # The tombstoned command keeps the fencing identity that produced it.
        assert after["fencing"] == before["fencing"]
        assert {
            row["initial_attempt_id"] for row in after["fencing"] if row["operation_id"] == JOB
        } == {"attempt-exec"}
        assert objects.versions == []
        # Only the content moved: the intent row is gone, the command body cleared.
        assert _read(
            sandbox, "SELECT count(*) AS n FROM speech_job_intents WHERE job_id = %s", (JOB,)
        ) == [{"n": 0}]
        assert after["leases"][0]["lease_state"] == "reserved"
        assert after["settlement"][0]["settled_at_us"] == 1
    finally:
        pool.close()


def test_the_schema_itself_forbids_an_unpinned_object_version(sandbox: Sandbox) -> None:
    """The pinned version cannot drift: the composite foreign key refuses it.

    The runtime check in ``complete_content_deletion`` is the second line of
    defence; the first is this constraint, so no reachable state has a live
    object version different from the one the accepted deletion pinned.
    """
    admin = _admin(sandbox.url)
    admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            admin.execute(
                "INSERT INTO speech_content_deletions (ledger_id, asset_id, sha256, "
                "storage_version_id, reason, deletion_state, blocked_reason, created_at_us) "
                "VALUES ('ledger-unpinned', %s, %s, 'minio-forged', 'confirmed_deletion', "
                "'queued', NULL, 1)",
                (ASSET, sha("a")),
            )
    finally:
        admin.rollback()
        admin.close()


async def test_a_concurrent_same_key_request_replays_the_winner(sandbox: Sandbox) -> None:
    """Two overlapping requests, one key: one purge, one durable answer.

    The winner is held inside its object purge -- after it owns the purge claim
    and before it can complete -- so the loser is *guaranteed* to enter the
    queued window while the claim is live. Without the claim the loser would
    purge the same pinned version a second time, which is exactly the assertion
    below.
    """
    app, pool, objects, _artifacts = _app(sandbox)
    plan = await _preview(app, "asset", ASSET, 3, "key-plan-concurrent")
    body = _confirm(plan)
    purging = threading.Event()
    allow_completion = threading.Event()
    responses: list[httpx.Response] = []

    def hold_purge(proof: VerifiedObject) -> None:
        purging.set()
        assert allow_completion.wait(timeout=30), "the purge was never released"

    objects.on_delete = hold_purge
    winner = threading.Thread(
        target=lambda: responses.append(asyncio.run(_execute(app, body, "key-exec-concurrent")))
    )
    try:
        winner.start()
        assert purging.wait(timeout=15), "the winner never reached its purge"
        # The loser enters while the claim is live: it must wait, not purge.
        loser = threading.Thread(
            target=lambda: responses.append(asyncio.run(_execute(app, body, "key-exec-concurrent")))
        )
        loser.start()
        # The loser is inside its wait on the claim now; release the winner.
        await asyncio.sleep(0.3)
        allow_completion.set()
        winner.join(timeout=30)
        loser.join(timeout=30)
        assert not winner.is_alive() and not loser.is_alive()
        assert [response.status_code for response in responses] == [202, 202], [
            response.text for response in responses
        ]
        ledger = _ledger(sandbox, "key-exec-concurrent")
        assert len(ledger) == 1
        assert ledger[0]["status"] == "succeeded"
        assert {response.json()["id"] for response in responses} == {ledger[0]["id"]}
        assert {response.json()["result"]["ledger_id"] for response in responses} == {
            ledger[0]["id"]
        }
        assert len({response.json()["result"]["completed_at"] for response in responses}) == 1
        assert len({response.text for response in responses}) == 1
        # The whole point: one pinned version, purged once.
        assert objects.versions == ["minio-asset-exec-1"]
    finally:
        allow_completion.set()
        pool.close()


def test_the_unique_key_race_falls_back_to_the_winning_row(sandbox: Sandbox) -> None:
    """A key race that reaches the insert resolves to the committed row.

    The winner's insert is held open until the loser has passed both reads and is
    blocked inside the unique index, so this reaches the UniqueViolation fallback
    instead of the ordinary replay path. A different intent under the same key is
    a conflict, not a second deletion.
    """
    _app_instance, pool, _objects, _artifact_objects = _app(sandbox)
    asset_plan = _plan(pool, "asset", ASSET, 3, "key-plan-race-fallback")
    job_plan = _plan(pool, "operation_content", JOB, 4, "key-plan-race-other")
    asset_request = _confirmation_of(asset_plan)
    job_request = _confirmation_of(job_plan)
    key_digest = hashlib.sha256(b"key-exec-race-fallback").hexdigest()
    store = PostgresDeletionExecutions(pool, timeout_seconds=5)
    outcome: list[BaseException] = []

    def lose() -> None:
        try:
            store.accept(
                job_request,
                key_digest=key_digest,
                intent_digest=deletion_intent_digest(job_request),
                purge_available=True,
                artifact_purge_available=True,
                request_id="race-fallback",
                workspace_id=WORKSPACE,
            )
        except BaseException as error:
            outcome.append(error)

    try:
        with pool.connection(timeout=5) as connection:
            transaction = _Transaction(connection, time.monotonic() + 5, WORKSPACE)
            accepted = _DeletionExecution(transaction, WORKSPACE).accept(
                asset_request,
                key_digest=key_digest,
                intent_digest=deletion_intent_digest(asset_request),
                purge_available=True,
                artifact_purge_available=True,
                request_id="race-fallback",
            )
            assert accepted.record.status == "queued" or accepted.record.status == "succeeded"
            loser = threading.Thread(target=lose)
            loser.start()
            # Give the loser time to pass its reads and block inside the index.
            deadline = time.monotonic() + 2
            while not _blocked_on_the_key(sandbox) and time.monotonic() < deadline:
                time.sleep(0.02)
            connection.commit()
        loser.join(timeout=30)
        assert not loser.is_alive()
        assert len(outcome) == 1, outcome
        assert isinstance(outcome[0], DeletionIdempotencyConflictError)
        ledger = _ledger(sandbox, "key-exec-race-fallback")
        assert len(ledger) == 1
        assert ledger[0]["plan_id"] == asset_plan.id
        assert ledger[0]["target_id"] == ASSET
    finally:
        pool.close()


async def test_a_writing_asset_is_refused_at_execution_time(sandbox: Sandbox) -> None:
    app, pool, objects, _artifacts = _app(sandbox)
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
    app, pool, objects, _artifacts = _app(sandbox)
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
        app, _, objects, artifacts = _app(sandbox, pool=pool)
        asset_plan = await _preview(app, "asset", ASSET, 3, "key-role-plan-asset")
        asset = await _execute(app, _confirm(asset_plan), "key-role-exec-asset")
        assert asset.status_code == 202, asset.text
        assert asset.json()["status"] == "succeeded"
        assert objects.versions == ["minio-asset-exec-1"]
        # The writer identity keeps the operation intent until it is the target.
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

        # The same identity owns the two target kinds this slice adds.
        _seed_unreferenced_revision(sandbox)
        artifact_plan = await _preview(app, "artifact", ARTIFACT, 1, "key-role-plan-artifact")
        artifact = await _execute(app, _confirm(artifact_plan), "key-role-exec-artifact")
        assert artifact.status_code == 202, artifact.text
        assert artifact.json()["status"] == "succeeded"
        assert artifacts.pinned == [("artifacts/artifact-exec-pg.wav", "minio-artifact-exec-1")]
        revision_plan = await _preview(app, "voice_revision", DRAFT, 2, "key-role-plan-rev")
        revision = await _execute(app, _confirm(revision_plan), "key-role-exec-rev")
        assert revision.status_code == 202, revision.text
        assert revision.json()["status"] == "succeeded"
        assert _read(
            sandbox,
            "SELECT revision FROM speech_voice_draft_revisions WHERE draft_id = %s",
            (DRAFT,),
        ) == [{"revision": "1"}]
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
        app, _, objects, _artifacts = _app(sandbox, pool=pool)
        plan = await _preview(app, "asset", ASSET, 3, "key-nogrant-plan")
        response = await _execute(app, _confirm(plan), "key-nogrant-exec")
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["code"] == "deletion_not_configured"
        assert error["retryable"] is False
        # Transport replay stays safe even though retrying cannot fix the gap.
        assert response.headers["x-should-retry"] == "true"
        assert "GRANT" in error["message"] or "apply" in error["message"]
        assert objects.versions == []
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_deletion_ledger") == [{"n": 0}]
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


async def test_artifact_execution_purges_the_pinned_version_and_marks_it_deleted(
    sandbox: Sandbox,
) -> None:
    """A committed Artifact is content: purge its pinned version, then record it."""
    app, pool, objects, artifacts = _app(sandbox)
    try:
        # An Artifact has no revision column; only revision 1 names a row.
        missing = await _post(
            app,
            "/v1/nexus/deletion-plans",
            _body("artifact", ARTIFACT, 2),
            "key-artifact-plan-2",
        )
        assert missing.status_code == 404, missing.text
        plan = await _preview(app, "artifact", ARTIFACT, 1, "key-artifact-plan")
        assert plan["eligible"] is True
        assert plan["target"]["kind"] == "artifact"
        assert plan["target"]["resource"]["revision"] == "1"

        response = await _execute(app, _confirm(plan), "key-artifact-exec")
        assert response.status_code == 202, response.text
        payload = response.json()
        _deletion_validator("Deletion").validate(payload)
        assert payload["status"] == "succeeded"
        assert payload["cleanup_state"] == "complete"
        assert payload["error"] is None
        assert payload["result"]["deleted"] == [plan["target"]]
        assert artifacts.pinned == [("artifacts/artifact-exec-pg.wav", "minio-artifact-exec-1")]
        # The asset object identity was never asked to purge the Artifact.
        assert objects.versions == []

        row = _read(
            sandbox,
            "SELECT artifact_state, deleted_at_us, sha256, object_key, storage_version_id "
            "FROM speech_artifacts WHERE artifact_id = %s",
            (ARTIFACT,),
        )
        assert len(row) == 1
        assert row[0]["artifact_state"] == "deleted"
        assert row[0]["deleted_at_us"] is not None
        assert row[0]["sha256"] == sha("9")
        assert row[0]["object_key"] == "artifacts/artifact-exec-pg.wav"
        assert row[0]["storage_version_id"] == "minio-artifact-exec-1"
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_deletion_ledger") == [{"n": 1}]
    finally:
        pool.close()


async def test_an_artifact_referenced_by_execution_evidence_is_blocked(
    sandbox: Sandbox,
) -> None:
    """The validation report of a retained execution is not deletable."""
    _seed_unreferenced_revision(sandbox)
    _seed_evidence_reporting_the_artifact(sandbox)
    app, pool, _objects, artifacts = _app(sandbox)
    try:
        plan = await _preview(app, "artifact", ARTIFACT, 1, "key-artifact-evidence-plan")
        assert plan["eligible"] is False
        assert [item["target"]["kind"] for item in plan["dependencies"]] == ["operation_content"]

        response = await _execute(app, _confirm(plan), "key-artifact-evidence-exec")
        assert response.status_code == 202, response.text
        payload = response.json()
        _deletion_validator("Deletion").validate(payload)
        assert payload["status"] == "blocked"
        assert payload["cleanup_state"] == "pending"
        assert payload["result"] is None
        assert payload["error"]["code"] == "deletion_target_in_use"
        assert payload["error"]["retryable"] is False
        assert REV2_JOB in payload["error"]["message"]
        assert artifacts.pinned == []
        assert _read(
            sandbox,
            "SELECT artifact_state FROM speech_artifacts WHERE artifact_id = %s",
            (ARTIFACT,),
        ) == [{"artifact_state": "ready"}]

        # The same key answers the same blocked record, not a second row.
        replay = await _execute(app, _confirm(plan), "key-artifact-evidence-exec")
        assert replay.json()["id"] == payload["id"]
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_deletion_ledger") == [{"n": 1}]
    finally:
        pool.close()


async def test_voice_revision_execution_deletes_only_that_revision(sandbox: Sandbox) -> None:
    _seed_unreferenced_revision(sandbox)
    app, pool, objects, artifacts = _app(sandbox)
    try:
        plan = await _preview(app, "voice_revision", DRAFT, 2, "key-revision-plan")
        assert plan["eligible"] is True
        assert plan["target"]["resource"] == {"id": DRAFT, "revision": "2"}

        response = await _execute(app, _confirm(plan), "key-revision-exec")
        assert response.status_code == 202, response.text
        payload = response.json()
        _deletion_validator("Deletion").validate(payload)
        assert payload["status"] == "succeeded"
        assert payload["result"]["deleted"] == [plan["target"]]
        assert objects.versions == []
        assert artifacts.pinned == []
        assert _read(
            sandbox,
            "SELECT revision FROM speech_voice_draft_revisions "
            "WHERE draft_id = %s ORDER BY revision",
            (DRAFT,),
        ) == [{"revision": "1"}]
        assert _read(
            sandbox, "SELECT head_revision FROM speech_voice_drafts WHERE draft_id = %s", (DRAFT,)
        ) == [{"head_revision": "1"}]
        # The generation job is execution evidence, not content of the revision.
        assert _read(sandbox, "SELECT job_id FROM speech_jobs WHERE job_id = %s", (REV2_JOB,)) == [
            {"job_id": REV2_JOB}
        ]

        # The same key repeats the same record, byte for byte: it says already
        # done (same id and completion time), not just done again.
        replay = await _execute(app, _confirm(plan), "key-revision-exec")
        assert replay.status_code == 202
        assert replay.json() == payload
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_deletion_ledger") == [{"n": 1}]
    finally:
        pool.close()


def _wait_for_lock(sandbox: Sandbox, query_fragment: str, wait_fragment: str) -> bool:
    """Observe one backend actually waiting on the named row lock."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = _read(
            sandbox,
            "SELECT count(*) AS n FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND strpos(query, %s) > 0 "
            "AND strpos(query, %s) > 0",
            (query_fragment, wait_fragment),
        )
        if rows and rows[0]["n"]:
            return True
        time.sleep(0.02)
    return False


async def test_voice_revision_deletion_serializes_with_a_concurrent_intent(
    sandbox: Sandbox,
) -> None:
    """The deletion locks the draft before it re-reads, so a racing intent wins or loses.

    The second connection holds the admission's ``FOR SHARE`` on the draft and has
    already read the revision, exactly where admission sits before it inserts its
    intent. The deletion must wait on ``FOR UPDATE``; when the intent commits, the
    post-lock re-read sees it and answers ``409 deletion_plan_changed``. Removing
    the deletion lock lets the DELETE commit first and leaves an intent pointing
    at a missing revision, which is the dangling state this test forbids.
    """
    _seed_unreferenced_revision(sandbox)
    app, pool, _objects, _artifacts = _app(sandbox)
    admission = psycopg.connect(sandbox.url, row_factory=dict_row)
    responses: list[httpx.Response] = []
    try:
        plan = await _preview(app, "voice_revision", DRAFT, 2, "key-race-plan")
        body = _confirm(plan)
        admission.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        admission.execute("SET TIME ZONE 'UTC'")
        admission.execute(
            "SELECT 1 FROM speech_voice_drafts WHERE draft_id = %s FOR SHARE", (DRAFT,)
        )
        admission.execute(
            "SELECT input_json FROM speech_voice_draft_revisions "
            "WHERE draft_id = %s AND revision = '2'",
            (DRAFT,),
        )
        thread = threading.Thread(
            target=lambda: responses.append(asyncio.run(_execute(app, body, "key-race-exec")))
        )
        thread.start()
        waited = _wait_for_lock(sandbox, "speech_voice_drafts", "FOR UPDATE")
        if not waited:
            # No lock means the deletion already won; let it finish before the
            # admission-shaped insert so the resulting dangling row is stable.
            thread.join(5)
            assert not thread.is_alive()
        admission.execute(
            "INSERT INTO speech_job_intents "
            "(job_id, request_digest, request_document, recorded_at_us) "
            "VALUES (%s, %s, %s, %s)",
            (
                REV2_JOB,
                sha("4"),
                '{"model":"tts-synthetic","input":{"voice_draft_id":"'
                + DRAFT
                + '","draft_revision":"2"}}',
                NOW_US,
            ),
        )
        admission.commit()
        thread.join(30)
        assert not thread.is_alive()
        assert len(responses) == 1
        assert responses[0].status_code == 409, responses[0].text
        assert responses[0].json()["error"]["code"] == "deletion_plan_changed"
        # The losing request left the revision for the intent it now names.
        assert _read(
            sandbox,
            "SELECT count(*) AS n FROM speech_voice_draft_revisions "
            "WHERE draft_id = %s AND revision = '2'",
            (DRAFT,),
        ) == [{"n": 1}]
        assert _read(
            sandbox,
            "SELECT count(*) AS n FROM speech_job_intents WHERE job_id = %s",
            (REV2_JOB,),
        ) == [{"n": 1}]
    finally:
        admission.rollback()
        admission.close()
        thread.join(5)
        pool.close()


async def test_a_voice_revision_content_change_is_refused(sandbox: Sandbox) -> None:
    """The digest pins the two content columns, not only the row identity."""
    _seed_unreferenced_revision(sandbox)
    app, pool, _objects, _artifacts = _app(sandbox)
    try:
        plan = await _preview(app, "voice_revision", DRAFT, 2, "key-content-plan")
        with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as admin:
            admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            admin.execute(
                "UPDATE speech_voice_draft_revisions SET input_json = %s "
                "WHERE draft_id = %s AND revision = '2'",
                ('{"text":"changed after preview"}', DRAFT),
            )
        response = await _execute(app, _confirm(plan), "key-content-exec")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "deletion_plan_changed"
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_deletion_ledger") == [{"n": 0}]
        assert _read(
            sandbox,
            "SELECT revision FROM speech_voice_draft_revisions "
            "WHERE draft_id = %s ORDER BY revision",
            (DRAFT,),
        ) == [{"revision": "1"}, {"revision": "2"}]
    finally:
        pool.close()


async def test_the_draft_head_revision_is_blocked_and_replays_idempotently(
    sandbox: Sandbox,
) -> None:
    app, pool, _objects, _artifacts = _app(sandbox)
    try:
        plan = await _preview(app, "voice_revision", DRAFT, 1, "key-head-plan")
        assert plan["eligible"] is False
        blocked = await _execute(app, _confirm(plan), "key-head-exec")
        assert blocked.status_code == 202, blocked.text
        payload = blocked.json()
        _deletion_validator("Deletion").validate(payload)
        assert payload["status"] == "blocked"
        assert payload["error"]["code"] == "deletion_target_in_use"
        assert "draft-exec-pg@1" in payload["error"]["message"]
        assert _read(
            sandbox,
            "SELECT revision FROM speech_voice_draft_revisions WHERE draft_id = %s",
            (DRAFT,),
        ) == [{"revision": "1"}]

        replay = await _execute(app, _confirm(plan), "key-head-exec")
        assert replay.status_code == 202
        assert replay.json()["id"] == payload["id"]
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_deletion_ledger") == [{"n": 1}]

        # Resolving the dependency queued a new preview; the old key stays blocked
        # and the new plan deletes the now-unreferenced revision.
        _seed_unreferenced_revision(sandbox)
        with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as admin:
            admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            admin.execute(
                "UPDATE speech_voice_drafts SET head_revision = '2' WHERE draft_id = %s",
                (DRAFT,),
            )
        fresh = await _preview(app, "voice_revision", DRAFT, 1, "key-head-plan-2")
        assert fresh["eligible"] is True
        done = await _execute(app, _confirm(fresh), "key-head-exec-2")
        assert done.status_code == 202, done.text
        assert done.json()["status"] == "succeeded"
        assert _read(
            sandbox,
            "SELECT revision FROM speech_voice_draft_revisions "
            "WHERE draft_id = %s ORDER BY revision",
            (DRAFT,),
        ) == [{"revision": "2"}]
    finally:
        pool.close()


async def test_a_voice_revision_moved_between_preview_and_execution_is_refused(
    sandbox: Sandbox,
) -> None:
    """The dependency set is part of the pinned scope, not a preview hint."""
    _seed_unreferenced_revision(sandbox)
    app, pool, _objects, _artifacts = _app(sandbox)
    try:
        plan = await _preview(app, "voice_revision", DRAFT, 2, "key-moved-plan")
        assert plan["eligible"] is True
        with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as admin:
            admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
            admin.execute(
                "UPDATE speech_voice_drafts SET head_revision = '2' WHERE draft_id = %s",
                (DRAFT,),
            )
        response = await _execute(app, _confirm(plan), "key-moved-exec")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "deletion_plan_changed"
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_deletion_ledger") == [{"n": 0}]
        assert _read(
            sandbox,
            "SELECT revision FROM speech_voice_draft_revisions "
            "WHERE draft_id = %s ORDER BY revision",
            (DRAFT,),
        ) == [{"revision": "1"}, {"revision": "2"}]
    finally:
        pool.close()


async def test_a_retained_job_intent_blocks_the_revision_it_names(sandbox: Sandbox) -> None:
    """#540 is not covered: the intent is not deleted, so the revision is blocked."""
    _seed_unreferenced_revision(sandbox)
    _seed_job_intent_referencing(sandbox)
    app, pool, _objects, _artifacts = _app(sandbox)
    try:
        plan = await _preview(app, "voice_revision", DRAFT, 2, "key-intent-plan")
        assert plan["eligible"] is False
        assert [item["target"]["kind"] for item in plan["dependencies"]] == ["operation_content"]
        response = await _execute(app, _confirm(plan), "key-intent-exec")
        assert response.status_code == 202, response.text
        payload = response.json()
        _deletion_validator("Deletion").validate(payload)
        assert payload["status"] == "blocked"
        assert payload["error"]["code"] == "deletion_target_in_use"
        assert REV2_JOB in payload["error"]["message"]
        assert _read(
            sandbox,
            "SELECT revision FROM speech_voice_draft_revisions "
            "WHERE draft_id = %s ORDER BY revision",
            (DRAFT,),
        ) == [{"revision": "1"}, {"revision": "2"}]
    finally:
        pool.close()


async def test_an_artifact_purge_failure_resumes_after_a_restart(sandbox: Sandbox) -> None:
    """The interrupted attempt leaves a queued row; a fresh service finishes it."""
    app, pool, _objects, artifacts = _app(
        sandbox, artifact_objects=FakeArtifactPurge(fail_first=True)
    )
    plan = await _preview(app, "artifact", ARTIFACT, 1, "key-artifact-restart-plan")
    first = await _execute(app, _confirm(plan), "key-artifact-restart-exec")
    assert first.status_code == 202, first.text
    queued = first.json()
    _deletion_validator("Deletion").validate(queued)
    assert queued["status"] == "queued"
    assert queued["cleanup_state"] == "pending"
    assert queued["result"] is None
    assert artifacts.pinned == []
    assert _read(
        sandbox, "SELECT artifact_state FROM speech_artifacts WHERE artifact_id = %s", (ARTIFACT,)
    ) == [{"artifact_state": "ready"}]
    ledger_id = queued["id"]
    pool.close()

    # A new pool and a new service object: the durable row, not process memory,
    # carries the replay identity and the pinned object version.
    restarted_app, restarted_pool, _objects, restarted_artifacts = _app(sandbox)
    try:
        resumed = await _execute(restarted_app, _confirm(plan), "key-artifact-restart-exec")
        assert resumed.status_code == 202, resumed.text
        payload = resumed.json()
        _deletion_validator("Deletion").validate(payload)
        assert payload["id"] == ledger_id
        assert payload["status"] == "succeeded"
        assert restarted_artifacts.pinned == [
            ("artifacts/artifact-exec-pg.wav", "minio-artifact-exec-1")
        ]
        assert _read(sandbox, "SELECT count(*) AS n FROM speech_deletion_ledger") == [{"n": 1}]
        assert _read(
            sandbox,
            "SELECT artifact_state FROM speech_artifacts WHERE artifact_id = %s",
            (ARTIFACT,),
        ) == [{"artifact_state": "deleted"}]
    finally:
        restarted_pool.close()


async def test_artifact_and_revision_deletion_leave_every_evidence_fact_intact(
    sandbox: Sandbox,
) -> None:
    _seed_unreferenced_revision(sandbox)
    before = _evidence(sandbox)
    jobs_before = _read(sandbox, "SELECT * FROM speech_jobs ORDER BY job_id")
    intents_before = _read(sandbox, "SELECT * FROM speech_job_intents ORDER BY job_id")
    events_before = _read(sandbox, "SELECT * FROM speech_job_events ORDER BY job_id, event_seq")
    app, pool, _objects, _artifacts = _app(sandbox)
    try:
        artifact_plan = await _preview(app, "artifact", ARTIFACT, 1, "key-evidence-artifact")
        assert (
            await _execute(app, _confirm(artifact_plan), "key-evidence-artifact")
        ).status_code == 202
        revision_plan = await _preview(app, "voice_revision", DRAFT, 2, "key-evidence-revision")
        assert (
            await _execute(app, _confirm(revision_plan), "key-evidence-revision")
        ).status_code == 202
        assert _evidence(sandbox) == before
        assert _read(sandbox, "SELECT * FROM speech_jobs ORDER BY job_id") == jobs_before
        assert _read(sandbox, "SELECT * FROM speech_job_intents ORDER BY job_id") == intents_before
        assert (
            _read(sandbox, "SELECT * FROM speech_job_events ORDER BY job_id, event_seq")
            == events_before
        )
    finally:
        pool.close()
