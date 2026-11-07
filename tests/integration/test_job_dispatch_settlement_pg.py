"""Production dispatch: stored-command plan, settlement and capacity return.

The gateway and publisher are fakes; everything on the SpeechNexus side is the
real admission, claim, planner, state store and LeaseService path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, cast

import httpx
import pytest
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_public_job_atomic_admission_pg import (
    BINDING,
    DIGEST,
    WORKSPACE,
    _job,
    _seed,
    _store,
)

from speechnexus_backend.job_cancellation_runtime import _PublicJobsSettlement
from speechnexus_backend.job_dispatch_runtime import (
    JobDispatchSettings,
    build_dispatch_dependencies,
)
from speechnexus_backend.job_dispatch_worker import _main
from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    JobCancellationReconciler,
)
from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchJob,
    DispatchLimits,
    DispatchProtocolError,
    DispatchRequestUnavailableError,
    ExecutionSubmission,
    PublishedResult,
    submit_intent_identity,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJob
from speechnexus_backend.speech.infrastructure.execution_gateway_adapter import (
    ExecutionGatewayAdapter,
)
from speechnexus_backend.speech.infrastructure.gateway_cancellation_confirmation import (
    GatewayCancellationConfirmer,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
    GatewayRejectedError,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_reconciliation_postgres import (
    PostgresCancellationReconciliationStore,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
    PostgresLeaseSettler,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.stored_command_planner import (
    StoredCommandPlanner,
)
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    require_complete_tts_roots,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

TOKEN = SecretStr("synthetic-dispatch-token-0001")


def _pool(admission_database: AdmissionDatabase) -> Any:
    pool = admission_database.runtime.pool
    assert pool is not None
    return pool


def _asset(db: Any) -> None:
    db.query(
        "INSERT INTO speech_assets (asset_id, workspace_id, purpose, asset_state, upload_phase, "
        "bytes, sha256, request_digest, object_key, media_type, duration_ms, sample_rate_hz, "
        "channels, created_at_us, expires_at_us) VALUES ('asset-1', %s, 'asr_input', 'ready', "
        "'settled', 1024, %s, %s, 'private/asset-1', 'audio/wav', 1000, 16000, 1, 1, 1000000)",
        (WORKSPACE, DIGEST, DIGEST),
    )


def _now_us(db: Any) -> int:
    row = db.query(
        "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
    )[0]
    return int(row["now_us"])


def _submit(store: Any, db: Any, **kwargs: Any) -> Any:
    """Create with a real submission time; the A2 fixture pins a future one."""
    job = _job(**kwargs)
    return store.create(
        replace(job, submission=replace(job.submission, submitted_at_us=_now_us(db)))
    )


def _legacy_voice_anchor(db: Any, job_id: str, request_digest: str) -> None:
    """The pre-admission shape: an operation anchor with no attempt or lease."""
    now = _now_us(db)
    db.query(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "idempotency_key_digest, state, status_version, created_at_us, updated_at_us) "
        "VALUES (%s,%s,'voice_design',%s,%s,%s,'pending',1,%s,%s)",
        (job_id, WORKSPACE, BINDING, request_digest, request_digest, now, now),
    )
    db.query(
        "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, initial_event_id, "
        "initial_event_digest, request_digest, claim_expires_at_us, job_state) "
        "VALUES (%s,%s,%s,%s,%s,%s,0,'pending')",
        (
            job_id,
            job_id,
            job_id + "-attempt",
            job_id + "-event",
            request_digest,
            request_digest,
        ),
    )
    db.query(
        "INSERT INTO admission_operations (operation_id, workspace_id, request_digest, "
        "request_document, initial_attempt_id, current_attempt_id, recorded_at_us, "
        "command_redacted_at_us, command_deletion_job_id, command_deletion_evidence_digest) "
        "VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s)",
        (
            job_id,
            WORKSPACE,
            request_digest,
            job_id + "-attempt",
            job_id + "-attempt",
            now,
            now,
            "job-deletion-synthetic",
            DIGEST,
        ),
    )
    assert db.query("SELECT 1 AS n FROM admission_attempts WHERE operation_id=%s", (job_id,)) == []


def _lease(db: Any, job_id: str) -> dict[str, Any]:
    rows = db.query(
        "SELECT lease_id, lease_state, cleanup_state FROM admission_leases WHERE operation_id=%s",
        (job_id,),
    )
    assert len(rows) == 1
    return cast(dict[str, Any], rows[0])


def _read(store: Any, job_id: str) -> PublicJob:
    job = store.get(job_id)
    assert job is not None
    return cast(PublicJob, job)


def _capacity_snapshot(db: Any) -> dict[str, Any]:
    quota = db.query(
        "SELECT held_attempts, quarantined_attempts FROM workspace_quotas WHERE workspace_id=%s",
        (WORKSPACE,),
    )[0]
    hosts = {
        row["host_node_id"]: {
            key: value for key, value in row.items() if key.startswith(("held_", "quarantined_"))
        }
        for row in db.query("SELECT * FROM host_nodes")
    }
    instances = {
        (row["instance_id"], row["instance_generation"]): {
            "held_slots": row["held_slots"],
            "quarantined_slots": row["quarantined_slots"],
        }
        for row in db.query("SELECT * FROM engine_instances")
    }
    return cast(dict[str, Any], {"quota": quota, "hosts": hosts, "instances": instances})


def _assert_capacity_returned(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    assert after == before


class _Publisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def publish(
        self, job: Any, submission: ExecutionSubmission, receipt: Mapping[str, Any]
    ) -> PublishedResult:
        self.calls.append(job.job_id)
        return PublishedResult("artifact-synthetic", "receipt-synthetic")


@dataclass
class _Gateway:
    """A fake gateway that records the B rows a real submit would write."""

    db: Any
    terminal: str = "succeeded"
    submit_calls: int = 0
    query_calls: int = 0
    last_submission: dict[str, Any] | None = None

    def _deadline_us(self, body: Mapping[str, Any]) -> int:
        rows = self.db.query(
            "SELECT deadline_at_us FROM admission_leases WHERE lease_id=%s", (body["lease_id"],)
        )
        assert len(rows) == 1
        return int(rows[0]["deadline_at_us"])

    def _record_succeeded(self, body: Mapping[str, Any]) -> None:
        operation = self.db.query(
            "SELECT request_document FROM admission_operations WHERE operation_id=%s",
            (body["operation_id"],),
        )[0]
        self.db.query(
            "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id, "
            "command_digest, request_digest, request_document, deadline_at_us, recorded_at_us, "
            "updated_at_us, execution_state, cleanup_state, engine_request_id, dispatched_at_us, "
            "terminated_at_us) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,1,'succeeded','complete',"
            "'engine-request-synthetic',1,1)",
            (
                body["submit_intent_id"],
                body["lease_id"],
                body["operation_id"],
                body["attempt_id"],
                body["command_digest"],
                json.loads(operation["request_document"])["gateway_request"]["command_digest"],
                operation["request_document"],
                self._deadline_us(body),
            ),
        )
        self.db.query(
            "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, evidence_kind, "
            "evidence_digest, observed_at_us, execution_stopped, resources_released) "
            "VALUES (%s,%s,'engine_terminal_response',%s,1,1,1)",
            ("stop-" + body["submit_intent_id"], body["submit_intent_id"], DIGEST),
        )

    def _record_failed(self, body: Mapping[str, Any]) -> None:
        operation = self.db.query(
            "SELECT request_document FROM admission_operations WHERE operation_id=%s",
            (body["operation_id"],),
        )[0]
        self.db.query(
            "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id, "
            "command_digest, request_digest, request_document, deadline_at_us, recorded_at_us, "
            "updated_at_us, execution_state, cleanup_state) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,1,1,'failed','not_required')",
            (
                body["submit_intent_id"],
                body["lease_id"],
                body["operation_id"],
                body["attempt_id"],
                body["command_digest"],
                json.loads(operation["request_document"])["gateway_request"]["command_digest"],
                operation["request_document"],
                self._deadline_us(body),
            ),
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/internal/v1/executions/submit":
            body = json.loads(request.content)
            self.submit_calls += 1
            self.last_submission = body
            if self.terminal == "succeeded":
                self._record_succeeded(body)
            else:
                self._record_failed(body)
            return httpx.Response(
                200,
                json={"submit_intent_id": body["submit_intent_id"], "execution_state": "executing"},
            )
        if request.method == "GET" and path.endswith("/staging-result"):
            submit_id = path.split("/")[-2]
            body = self.last_submission or {}
            return httpx.Response(
                200,
                json={
                    "submit_intent_id": submit_id,
                    "request_digest": body.get("command_digest", DIGEST),
                    "staging_result": {
                        "request_digest": body.get("command_digest", DIGEST),
                        "reference": {
                            "asset_or_artifact_id": "artifact-staging",
                            "workspace_id": WORKSPACE,
                            "storage_bucket": "synthetic",
                            "object_key": "synthetic/result",
                            "content_sha256": body.get("command_digest", DIGEST),
                            "byte_length": 32,
                        },
                        "storage_version": "version-1",
                        "media_type": "audio/wav",
                    },
                },
            )
        if request.method == "GET" and path.startswith("/internal/v1/executions/"):
            submit_id = path.split("/")[-1]
            self.query_calls += 1
            if self.submit_calls == 0:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "control_execution_not_found",
                            "message": "no",
                            "retryable": False,
                            "recovery": "none",
                        }
                    },
                )
            document: dict[str, Any] = {
                "submit_intent_id": submit_id,
                "execution_state": self.terminal,
            }
            if self.terminal != "succeeded":
                document["error_code"] = "engine_execution_failed"
            return httpx.Response(200, json=document)
        raise AssertionError("unexpected gateway path: " + request.method + " " + path)


def _dispatcher(
    pool: SpeechPool,
    db: Any,
    gateway: _Gateway,
    publisher: _Publisher,
    *,
    kinds: tuple[str, ...] = ("asr", "tts"),
    settler: PostgresLeaseSettler | None = None,
    now_us: Any = None,
) -> JobDispatcher:
    client = ExecutionGatewayAdapter(
        GatewayExecutionClient(
            base_url="http://127.0.0.1:9999",
            credential=TOKEN,
            timeout_seconds=1,
            max_retries=0,
            backoff_seconds=0,
            _transport=httpx.MockTransport(gateway.handler),
        )
    )
    dispatcher = JobDispatcher(
        PostgresDispatchQueue(pool, kinds=kinds, require_admission=True, timeout_seconds=3),
        PostgresDispatchStateStore(pool, timeout_seconds=3),
        StoredCommandPlanner(pool, timeout_seconds=3),
        client,
        publisher,
        limits=DispatchLimits(poll_seconds=0.01, job_timeout_seconds=30),
        now_us=now_us,
    )
    dispatcher.settle_capacity_through(settler or PostgresLeaseSettler(pool, timeout_seconds=3))
    return dispatcher


async def test_asr_success_settles_the_job_and_releases_capacity(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    baseline = _capacity_snapshot(db)
    _asset(db)
    store = _store(admission_database)
    created = _submit(store, db, kind="asr", key="dispatch-asr-success")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id
    job = _read(store, job_id)

    gateway, publisher = _Gateway(db), _Publisher()
    dispatcher = _dispatcher(_pool(admission_database), db, gateway, publisher)
    batch = await dispatcher.run_once("worker-a3")

    assert batch.claimed == 1 and batch.succeeded == 1, batch
    assert gateway.submit_calls >= 1
    assert publisher.calls == [job_id]
    assert _read(store, job_id).state == "succeeded"
    stored = _lease(db, job_id)
    assert stored["lease_state"] == "released" and stored["cleanup_state"] == "complete"
    _assert_capacity_returned(baseline, _capacity_snapshot(db))
    # A second settlement of the same operation is a replay, not a second decrement.
    assert await PostgresLeaseSettler(_pool(admission_database)).release(job_id) is True
    _assert_capacity_returned(baseline, _capacity_snapshot(db))
    assert job.state == "pending"


async def test_tts_engine_failure_releases_capacity_through_the_stop_guard(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    baseline = _capacity_snapshot(db)
    created = _submit(_store(admission_database), db, kind="tts", key="dispatch-tts-failed")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id

    gateway = _Gateway(db, terminal="failed")
    dispatcher = _dispatcher(_pool(admission_database), db, gateway, _Publisher(), kinds=("tts",))
    batch = await dispatcher.run_once("worker-a3")

    assert batch.claimed == 1 and batch.failed == 1, batch
    read = _read(_store(admission_database), job_id)
    assert read.state == "failed" and read.error_code == "engine_execution_failed"
    assert _lease(db, job_id)["lease_state"] == "released"
    _assert_capacity_returned(baseline, _capacity_snapshot(db))


async def test_past_deadline_and_gateway_not_found_settle_without_submitting(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _asset(db)
    baseline = _capacity_snapshot(db)
    db.query(
        "UPDATE speech_binding_revisions SET definition_json=jsonb_set("
        "definition_json::jsonb, '{execution_defaults,asr,timeout_seconds}', '0.05')::text "
        "WHERE binding_id=%s AND revision='1'",
        (BINDING,),
    )
    created = _submit(_store(admission_database), db, kind="asr", key="dispatch-asr-expired")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id
    await asyncio.sleep(0.2)

    gateway = _Gateway(db)
    dispatcher = _dispatcher(_pool(admission_database), db, gateway, _Publisher())
    batch = await dispatcher.run_once("worker-a3")

    assert batch.claimed == 1 and batch.failed == 1, batch
    assert gateway.submit_calls == 0
    read = _read(_store(admission_database), job_id)
    assert read.state == "failed" and read.error_code == "execution_deadline_expired"
    assert _lease(db, job_id)["lease_state"] == "released"
    _assert_capacity_returned(baseline, _capacity_snapshot(db))


async def test_a_released_lease_is_never_claimed_by_the_admission_lane(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _asset(db)
    created = _submit(_store(admission_database), db, kind="asr", key="dispatch-asr-released")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id
    # No submit exists, so this is the never-dispatched release path.
    assert await PostgresLeaseSettler(_pool(admission_database)).release(job_id) is True
    assert _lease(db, job_id)["lease_state"] == "released"

    claims = await PostgresDispatchQueue(
        _pool(admission_database),
        kinds=("asr", "tts"),
        require_admission=True,
        timeout_seconds=3,
    ).claim_due("worker-a3", 30_000, 10)

    assert claims == []


async def test_voice_anchor_rows_are_never_claimed_by_the_admission_lane(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _asset(db)
    voice_id = "job-" + "a" * 48
    _legacy_voice_anchor(db, voice_id, "sha256:" + "b" * 64)
    store = _store(admission_database)
    asr = _submit(store, db, kind="asr", key="dispatch-asr-lane")
    assert asr.outcome == "created", asr.error_code

    claims = await PostgresDispatchQueue(
        _pool(admission_database),
        kinds=("voice_design", "asr"),
        require_admission=True,
        timeout_seconds=3,
    ).claim_due("worker-a3", 30_000, 10)

    assert [claim.job.job_id for claim in claims] == [asr.job.job_id]
    attempts = db.query(
        "SELECT operation_id FROM admission_attempts WHERE operation_id LIKE 'job-%'"
    )
    assert {row["operation_id"] for row in attempts} == {asr.job.job_id}
    leases = db.query("SELECT operation_id FROM admission_leases WHERE operation_id LIKE 'job-%'")
    assert {row["operation_id"] for row in leases} == {asr.job.job_id}


def _command(db: Any, job_id: str) -> tuple[dict[str, Any], str]:
    row = db.query(
        "SELECT request_document FROM admission_operations WHERE operation_id=%s", (job_id,)
    )[0]
    document: dict[str, Any] = json.loads(row["request_document"])
    return document, row["request_document"]


def _rewrite_command(db: Any, job_id: str, document: dict[str, Any]) -> None:
    wire = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    digest = "sha256:" + hashlib.sha256(wire.encode("utf-8")).hexdigest()
    db.query(
        "UPDATE admission_operations SET request_document=%s, request_digest=%s "
        "WHERE operation_id=%s",
        (wire, digest, job_id),
    )
    db.query("UPDATE coordination_jobs SET request_digest=%s WHERE job_id=%s", (digest, job_id))


async def test_stored_command_outside_the_frozen_contract_is_refused(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _asset(db)
    created = _submit(_store(admission_database), db, kind="asr", key="dispatch-asr-shape")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id
    document, _ = _command(db, job_id)
    del document["gateway_request"]["max_output_audio_ms"]
    _rewrite_command(db, job_id, document)

    planner = StoredCommandPlanner(_pool(admission_database))
    stored = _read(_store(admission_database), job_id)
    with pytest.raises(DispatchProtocolError):
        await planner.plan(cast(DispatchJob, stored), submit_intent_identity(job_id))


async def test_stored_command_identity_must_match_the_dispatch_identity(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _asset(db)
    created = _submit(_store(admission_database), db, kind="asr", key="dispatch-asr-id")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id

    planner = StoredCommandPlanner(_pool(admission_database))
    stored = _read(_store(admission_database), job_id)
    with pytest.raises(DispatchProtocolError):
        await planner.plan(cast(DispatchJob, stored), "submit-" + "f" * 32)


async def test_planner_refuses_a_redacted_command(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _asset(db)
    created = _submit(_store(admission_database), db, kind="asr", key="dispatch-asr-redacted")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id
    db.query(
        "UPDATE admission_operations SET request_document=NULL, "
        "command_redacted_at_us=recorded_at_us, command_deletion_job_id=%s, "
        "command_deletion_evidence_digest=%s WHERE operation_id=%s",
        ("job-deletion-synthetic", DIGEST, job_id),
    )
    stored = _read(_store(admission_database), job_id)
    with pytest.raises((DispatchRequestUnavailableError, DispatchProtocolError)):
        await StoredCommandPlanner(_pool(admission_database)).plan(
            cast(DispatchJob, stored), submit_intent_identity(job_id)
        )


async def test_worker_main_assembles_the_lane_and_settles_one_round(
    admission_database: AdmissionDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import speechnexus_backend.job_dispatch_runtime as runtime_module

    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    baseline = _capacity_snapshot(db)
    _asset(db)
    created = _submit(_store(admission_database), db, kind="asr", key="dispatch-main-asr")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id

    publisher = _Publisher()
    monkeypatch.setattr(runtime_module, "build_result_publisher", lambda *a, **k: publisher)
    foundation = admission_database.runtime.foundation
    settings = JobDispatchSettings(
        process_role="job-worker",
        node_id="node-a",
        instance_id="worker-a",
        database_url=SecretStr(
            make_conninfo(db.url, user=foundation.speech_role, password="synthetic_foundation_test")
        ),
        database_schema=db.schema,
        gateway_base_url="http://127.0.0.1:9999",
        gateway_credential=SecretStr("synthetic-dispatch-token-0001"),
        gateway_timeout_seconds=1,
        batch_size=4,
        lease_ms=30_000,
        poll_seconds=0.01,
        job_timeout_seconds=30,
        max_attempts=3,
        interval_seconds=0.1,
        storage_timeout_seconds=3,
        dispatch_kinds="asr,tts",
        object_storage_endpoint="http://127.0.0.1:9000",
        artifacts_bucket="artifacts-synthetic",
        execution_result_bucket="results-synthetic",
        object_storage_access_key=SecretStr("access"),
        object_storage_secret_key=SecretStr("secret"),
    )
    gateway = _Gateway(db)
    adapter = ExecutionGatewayAdapter(
        GatewayExecutionClient(
            base_url="http://127.0.0.1:9999",
            credential=TOKEN,
            timeout_seconds=1,
            max_retries=0,
            backoff_seconds=0,
            _transport=httpx.MockTransport(gateway.handler),
        )
    )
    dependencies = build_dispatch_dependencies(settings)
    try:
        await _main(settings, dependencies, once=True, gateway=adapter)
    finally:
        await adapter.aclose()

    assert publisher.calls == [job_id]
    assert _read(_store(admission_database), job_id).state == "succeeded"
    assert _lease(db, job_id)["lease_state"] == "released"
    _assert_capacity_returned(baseline, _capacity_snapshot(db))
    document = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert document["event"] == "job_dispatch_batch"
    assert document["claimed"] == 1 and document["succeeded"] == 1


async def test_preflight_still_covers_live_public_asr_and_tts(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    role = admission_database.runtime.foundation.speech_role
    db.query(f'GRANT SELECT ON "{db.schema}".speech_tts_settlement TO "{role}"')
    created = _submit(_store(admission_database), db, kind="tts", key="dispatch-preflight-tts")
    assert created.outcome == "created", created.error_code
    # A live public TTS lease is owned by the dispatcher and needs no root.
    require_complete_tts_roots(_pool(admission_database))
    # A redacted public command keeps no kind to match; the lease identity must
    # still expose the leaked capacity once the deadline has long passed.
    db.query(
        "UPDATE admission_operations SET request_document=NULL, "
        "command_redacted_at_us=recorded_at_us, command_deletion_job_id=%s, "
        "command_deletion_evidence_digest=%s WHERE operation_id=%s",
        ("job-deletion-preflight", DIGEST, created.job.job_id),
    )
    db.query("UPDATE admission_leases SET deadline_at_us=1")
    with pytest.raises(ValueError, match="reconcile first"):
        require_complete_tts_roots(_pool(admission_database))
    db.query("UPDATE admission_leases SET deadline_at_us=%s", (2**62,))
    require_complete_tts_roots(_pool(admission_database))


# The documented operator query: the same public predicate as the preflight,
# deliberately without a Job-state whitelist so a cancelling or running Job that
# still holds expired capacity is visible too.
_UNSETTLED_LEASES = (
    "SELECT l.lease_id, l.operation_id, l.lease_state, l.deadline_at_us "
    "FROM admission_leases AS l "
    "CROSS JOIN (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 "
    "AS BIGINT) AS now_us) AS clock "
    "WHERE l.lease_state <> 'released' "
    "AND l.operation_id LIKE 'job-%' "
    "AND l.deadline_at_us <= clock.now_us - 60000000 "
    "ORDER BY l.lease_id"
)


async def test_cancellation_settlement_returns_the_admission_lease(
    admission_database: AdmissionDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal cancellation must not leak the attempt and physical slots.

    The admission lease is the only capacity record; a cancelled Job that keeps
    it would eventually drain the quota and make the API refuse to start once the
    frozen deadline passed. The documented operator query must also see the
    cancelled-but-held row while it lasts.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    pool = _pool(admission_database)
    _asset(db)
    db.query(
        f'GRANT SELECT ON "{db.schema}".speech_tts_settlement '
        f'TO "{admission_database.runtime.foundation.speech_role}"'
    )
    baseline = _capacity_snapshot(db)
    store = _store(admission_database)
    jobs = PublicJobs(store, store)
    created = _submit(store, db, kind="asr", key="dispatch-cancel-release")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id
    lease_id = _lease(db, job_id)["lease_id"]
    assert _lease(db, job_id)["lease_state"] == "reserved"

    jobs.advance(job_id, "running")
    jobs.cancel(job_id, request_id="req-cancel-release", idempotency_key=None)
    db.query(
        "INSERT INTO speech_job_cancellation_not_submitted (job_id, observed_at_us, observer) "
        "VALUES (%s, %s, 'worker-a')",
        (job_id, _now_us(db)),
    )
    past = _now_us(db) - 61_000_000
    db.query(
        "UPDATE admission_leases SET deadline_at_us = %s WHERE operation_id = %s", (past, job_id)
    )
    # The leak as measured before the fix: settlement alone leaves the lease held,
    # the documented query hides cancelled rows, and a restart is refused.
    current = jobs.get(job_id)
    assert current is not None
    assert (
        jobs.settle_cancelled(job_id, expected_version=current.status_version).state == "cancelled"
    )
    assert _lease(db, job_id)["lease_state"] == "reserved"
    assert db.query(_UNSETTLED_LEASES) == [
        {
            "lease_id": lease_id,
            "operation_id": job_id,
            "lease_state": "reserved",
            "deadline_at_us": past,
        }
    ]
    with pytest.raises(ValueError, match="reconcile first"):
        require_complete_tts_roots(pool)

    class MissingClient:
        async def __aenter__(self) -> MissingClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def query_document(self, submit_intent_id: str) -> dict[str, Any]:
            raise GatewayRejectedError(
                "missing",
                request_may_have_run=False,
                status_code=404,
                gateway_code="control_execution_not_found",
            )

    monkeypatch.setattr(
        GatewayExecutionClient,
        "from_settings",
        classmethod(lambda cls, settings: MissingClient()),
    )
    capacity = PostgresLeaseSettler(pool, timeout_seconds=3)
    settlement = _PublicJobsSettlement(jobs, capacity.release_sync)
    reconciler = JobCancellationReconciler(
        store=PostgresCancellationReconciliationStore(pool),
        confirmer=GatewayCancellationConfirmer(cast(Any, object()), settlement),
        min_age_seconds=0,
        max_attempts=3,
    )
    batch = await reconciler.run_once(now_us=_now_us(db) + 1_000_000)

    assert batch.claimed == 1 and batch.settled == 1
    assert _read(store, job_id).state == "cancelled"
    assert _lease(db, job_id) == {
        "lease_id": lease_id,
        "lease_state": "released",
        "cleanup_state": "complete",
    }
    _assert_capacity_returned(baseline, _capacity_snapshot(db))
    assert capacity.release_sync(job_id) is True
    assert _capacity_snapshot(db) == baseline
    assert db.query(_UNSETTLED_LEASES) == []
    # A restarted API accepts the long-expired deadline: nothing is held.
    require_complete_tts_roots(pool)


async def test_the_operator_query_finds_a_cancelling_lease_that_blocks_restart(
    admission_database: AdmissionDatabase,
) -> None:
    """The preflight refuses a restart on any expired held public lease.

    A cancelled acceptance that has not yet settled stays in ``cancelling`` with
    the lease still held, and a pending/running Job can be in the same shape.
    The documented query must therefore match the preflight predicate instead of
    listing terminal states, otherwise an operator sees an empty result while the
    API refuses to start.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _asset(db)
    db.query(
        f'GRANT SELECT ON "{db.schema}".speech_tts_settlement '
        f'TO "{admission_database.runtime.foundation.speech_role}"'
    )
    store = _store(admission_database)
    jobs = PublicJobs(store, store)
    created = _submit(store, db, kind="asr", key="dispatch-cancel-held")
    assert created.outcome == "created", created.error_code
    job_id = created.job.job_id
    jobs.advance(job_id, "running")
    jobs.cancel(job_id, request_id="req-cancel-held", idempotency_key=None)
    past = _now_us(db) - 61_000_000
    db.query(
        "UPDATE admission_leases SET deadline_at_us = %s WHERE operation_id = %s", (past, job_id)
    )

    assert _read(store, job_id).state == "cancelling"
    assert _lease(db, job_id)["lease_state"] == "reserved"
    rows = db.query(_UNSETTLED_LEASES)
    assert [(row["operation_id"], row["lease_state"]) for row in rows] == [(job_id, "reserved")]
    assert rows[0]["deadline_at_us"] == past
    with pytest.raises(ValueError, match="reconcile first"):
        require_complete_tts_roots(_pool(admission_database))
