"""Real PG/InnoDB observation transactions; readiness/lease fixtures are synthetic."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pymysql
import pytest
from tests.fake.runtime_intents import COMMAND, HOST, PLAN, REQUEST
from tests.integration.test_coordination_foundation import Foundation, UpgradeSandbox, seed
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_coordination_foundation import upgrade_sandbox as upgrade_sandbox
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import RuntimeDatabase
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database

from speechnexus_backend.speech.application.runtime_intents import RuntimeService
from speechnexus_backend.speech.domain.runtime_intents import RuntimeIntent
from speechnexus_backend.speech.domain.runtime_observations import (
    ObserveRuntimeIntent,
    VerifiedRuntimeReadiness,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.runtime_codec import (
    request_from_wire,
    request_to_wire,
    validate_original_observation,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    observation_for_boundary,
    prepare_observation,
)
from speechnexus_backend.speech.infrastructure.runtime_postgres import PostgresRuntimeTransactions
from speechnexus_backend.speech.interfaces.runtime_transactions import RuntimeTransactionBoundary

pytestmark = pytest.mark.integration


def _timestamp(at_us: int) -> str:
    return (
        (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=at_us))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _wire(
    runtime: RuntimeDatabase,
    request: RuntimeIntent = REQUEST,
    *,
    state: str = "succeeded",
    observed: str = "ready",
) -> dict[str, Any]:
    now = runtime.now()
    value = request_to_wire(request)
    value.pop("deadline_at")
    value.update(
        intent_state=state,
        observed_instance_state=observed,
        created_at=_timestamp(now - 2000),
        updated_at=_timestamp(now - 1000),
    )
    if state == "failed":
        value["error_code"] = "runtime_start_failed"
    if request.action == "stop" and state == "succeeded":
        value["stop_evidence"] = {
            "process_or_container_exited": True,
            "resources_released_confirmed": True,
            "verified_at": value["updated_at"],
        }
    return value


def _receipt(
    runtime: RuntimeDatabase, request: RuntimeIntent = REQUEST
) -> VerifiedRuntimeReadiness:
    now = runtime.now()
    return VerifiedRuntimeReadiness(
        request.binding,
        request.instance_id,
        request.instance_generation,
        request.fencing_token,
        PLAN.topology_digest,
        PLAN.qualification_digest,
        "sha256:" + "e" * 64,
        now - 1000,
        now + 60000000,
    )


def _command(
    wire: dict[str, Any],
    *,
    instance_version: int = 1,
    intent_version: int = 1,
    receipt: VerifiedRuntimeReadiness | None = None,
) -> ObserveRuntimeIntent:
    prepared = prepare_observation(wire)
    return ObserveRuntimeIntent(
        prepared.observation,
        instance_version,
        intent_version,
        COMMAND.owner_id,
        prepared.document,
        receipt,
    )


def _boundary(runtime: RuntimeDatabase) -> RuntimeTransactionBoundary:
    return RuntimeTransactionBoundary(
        RuntimeService(runtime.port),
        owner_id=COMMAND.owner_id,
        decode_request=request_from_wire,
        render_request=request_to_wire,
        decode_observation=validate_original_observation,
        parse_observation=observation_for_boundary,
    )


def _prepare_ready(runtime: RuntimeDatabase) -> dict[str, Any]:
    assert runtime.port.create_runtime_intent(COMMAND).outcome == "committed"
    wire = _wire(runtime)
    assert (
        runtime.port.observe_runtime(_command(wire, receipt=_receipt(runtime))).outcome
        == "committed"
    )
    return wire


def test_loading_then_qualified_ready_records_original_response_and_full_provenance(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    assert runtime.port.create_runtime_intent(COMMAND).outcome == "committed"
    wire = _wire(runtime, state="pending", observed="loading")
    event = db.query("SELECT * FROM coordination_outbox")
    result = _boundary(runtime).observe_runtime(
        {"observation": wire, "expected_instance_version": "1", "expected_intent_version": "1"},
        readiness=None,
    )
    assert result["outcome"] == "committed" and result["value"]["runtime"] == wire
    assert db.query("SELECT held_unified_physical_memory_bytes AS n FROM host_nodes") == [{"n": 25}]
    wire = {
        **wire,
        "intent_state": "succeeded",
        "observed_instance_state": "ready",
        "updated_at": _timestamp(runtime.now() - 1000),
    }
    receipt = _receipt(runtime)
    ready = runtime.port.observe_runtime(
        _command(wire, instance_version=2, intent_version=2, receipt=receipt)
    )
    assert (
        ready.outcome == "committed"
        and ready.fact is not None
        and ready.fact.readiness_document is not None
    )
    instance = db.query(
        "SELECT * FROM engine_instances WHERE instance_id = %s", (REQUEST.instance_id,)
    )[0]
    assert instance["instance_state"] == "ready" and instance["base_budget_kind"] == "steady"
    assert instance["ready_at_us"] == ready.fact.updated_at_us
    host = db.query("SELECT * FROM host_nodes")[0]
    assert tuple(host["held_" + name] for name in DIMENSIONS) == HOST.held.add(PLAN.steady).values
    assert tuple(host["quarantined_" + name] for name in DIMENSIONS) == HOST.quarantined.values
    stored = json.loads(ready.fact.readiness_document)
    assert (
        stored["evidence_digest"] == receipt.evidence_digest
        and stored["qualification_digest"] == PLAN.qualification_digest
    )
    assert json.loads(ready.fact.observation_document or "null") == wire
    snapshot = runtime.snapshot()
    db.query("UPDATE resource_pools SET owner_expires_at_us = 1")
    replay = runtime.port.observe_runtime(
        _command({**wire, "idempotent_replay": True}, instance_version=2, intent_version=2)
    )
    assert replay.outcome == "replayed" and replay.fact == ready.fact
    assert runtime.port.query_runtime_intent(REQUEST.identity).fact == ready.fact
    assert runtime.snapshot() == snapshot and db.query("SELECT * FROM coordination_outbox") == event


@pytest.mark.parametrize("bad", ["missing", "generation", "gateway", "qualification", "expiry"])
def test_ready_cannot_release_peak_without_matching_live_provider_receipt(
    runtime_database: RuntimeDatabase, bad: str
) -> None:
    runtime = runtime_database
    runtime.port.create_runtime_intent(COMMAND)
    receipt: VerifiedRuntimeReadiness | None = _receipt(runtime)
    assert receipt is not None
    if bad == "missing":
        receipt = None
    elif bad == "generation":
        receipt = replace(receipt, instance_generation=2)
    elif bad == "gateway":
        receipt = replace(receipt, binding=replace(receipt.binding, gateway_id="other-gateway"))
    elif bad == "qualification":
        receipt = replace(receipt, qualification_digest="sha256:" + "f" * 64)
    else:
        receipt = replace(receipt, expires_at_us=runtime.now())
    snapshot = runtime.snapshot()
    assert (
        runtime.port.observe_runtime(_command(_wire(runtime), receipt=receipt)).error_code
        == "reconciliation_required"
    )
    assert runtime.snapshot() == snapshot


@pytest.mark.parametrize(
    "field,value",
    [
        ("pool_id", "asr-realtime"),
        ("gateway_id", "other-gateway"),
        ("approved_template_id", "other-template"),
        ("binding_revision_id", "other-binding"),
        ("manifest_id", "other-manifest"),
        ("config_digest", "sha256:" + "f" * 64),
        ("instance_generation", 2),
        ("fencing_token", 8),
    ],
)
def test_schema_valid_identity_substitutions_cannot_change_budget(
    runtime_database: RuntimeDatabase, field: str, value: Any
) -> None:
    runtime = runtime_database
    runtime.port.create_runtime_intent(COMMAND)
    wire = {**_wire(runtime), field: value}
    snapshot = runtime.snapshot()
    assert (
        runtime.port.observe_runtime(_command(wire, receipt=_receipt(runtime))).error_code
        == "invalid_runtime_request"
    )
    assert runtime.snapshot() == snapshot


def test_recorded_creation_peak_time_and_terminal_facts_cannot_be_rewritten(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime = runtime_database
    runtime.port.create_runtime_intent(COMMAND)
    wire = _wire(runtime, state="pending", observed="loading")
    wire["observed_peak_memory"] = {
        "unified_memory_bytes": 20,
        "device_memory_bytes": 0,
        "host_memory_bytes": 0,
    }
    assert runtime.port.observe_runtime(_command(wire)).outcome == "committed"
    for changes, code in (
        ({"created_at": _timestamp(runtime.now() - 1000000)}, "intent_conflict"),
        ({"updated_at": wire["created_at"]}, "version_conflict"),
        ({"observed_peak_memory": None}, "intent_conflict"),
    ):
        snapshot = runtime.snapshot()
        result = runtime.port.observe_runtime(
            _command({**wire, **changes}, instance_version=2, intent_version=2)
        )
        assert result.error_code == code and runtime.snapshot() == snapshot
    wire = {
        **wire,
        "intent_state": "succeeded",
        "observed_instance_state": "ready",
        "updated_at": _timestamp(runtime.now() - 1000),
    }
    assert (
        runtime.port.observe_runtime(
            _command(wire, instance_version=2, intent_version=2, receipt=_receipt(runtime))
        ).outcome
        == "committed"
    )
    snapshot = runtime.snapshot()
    changed = {
        **wire,
        "intent_state": "failed",
        "observed_instance_state": "failed",
        "error_code": "runtime_start_failed",
    }
    assert (
        runtime.port.observe_runtime(
            _command(changed, instance_version=3, intent_version=3)
        ).error_code
        == "intent_conflict"
    )
    assert runtime.snapshot() == snapshot


def test_superseded_ready_quarantines_until_current_fence_stop_and_does_not_restore_host(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    runtime.port.create_runtime_intent(COMMAND)
    db.query("UPDATE resource_pools SET fencing_token = 8, owner_state = 'reconciling_takeover'")
    assert runtime.port.observe_runtime(_command(_wire(runtime))).outcome == "committed"
    host = db.query("SELECT * FROM host_nodes")[0]
    assert (
        host["held_unified_physical_memory_bytes"] == 5
        and host["quarantined_unified_physical_memory_bytes"] == 27
    )
    assert host["node_state"] == "quarantined"
    instance = db.query(
        "SELECT * FROM engine_instances WHERE instance_id=%s", (REQUEST.instance_id,)
    )[0]
    assert (
        instance["base_budget_kind"] == "q_peak"
        and instance["ready_at_us"] is None
        and instance["last_fencing_token"] == 7
    )
    stop = replace(
        COMMAND,
        request=replace(REQUEST, action="stop", intent_id="runtime-stop", fencing_token=8),
        expected_head_version=1,
        approved_plan=None,
    )
    assert runtime.port.create_runtime_intent(stop).outcome == "committed"
    result = runtime.port.observe_runtime(
        _command(_wire(runtime, stop.request, observed="absent"), instance_version=3)
    )
    assert result.outcome == "committed"
    host = db.query("SELECT * FROM host_nodes")[0]
    assert (
        host["quarantined_unified_physical_memory_bytes"] == 7
        and host["held_unified_physical_memory_bytes"] == 5
        and host["node_state"] == "quarantined"
    )
    assert db.query(
        "SELECT instance_state,base_budget_kind FROM engine_instances WHERE instance_id=%s",
        (REQUEST.instance_id,),
    ) == [{"instance_state": "stopped", "base_budget_kind": "released"}]


def test_quarantined_prepare_can_record_terminal_ready_without_unquarantining(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    runtime.port.create_runtime_intent(COMMAND)
    wire = _wire(runtime, state="quarantined", observed="quarantined")
    assert runtime.port.observe_runtime(_command(wire)).outcome == "committed"
    snapshot = db.query("SELECT * FROM host_nodes")
    wire = {
        **wire,
        "intent_state": "succeeded",
        "observed_instance_state": "ready",
        "updated_at": _timestamp(runtime.now() - 1000),
    }
    result = runtime.port.observe_runtime(_command(wire, instance_version=2, intent_version=2))
    assert (
        result.outcome == "committed"
        and result.fact is not None
        and result.fact.intent_state == "succeeded"
    )
    assert db.query("SELECT * FROM host_nodes") == snapshot
    assert db.query(
        "SELECT instance_state,base_budget_kind,ready_at_us FROM engine_instances"
        " WHERE instance_id=%s",
        (REQUEST.instance_id,),
    ) == [{"instance_state": "quarantined", "base_budget_kind": "q_peak", "ready_at_us": None}]


def test_over_peak_disables_host_but_confirmed_stop_can_release_actual_base(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    runtime.port.create_runtime_intent(COMMAND)
    db.query("UPDATE host_nodes SET quarantine_threshold_ppm=1000000")
    wire = _wire(runtime)
    wire["observed_peak_memory"] = {
        "unified_memory_bytes": 21,
        "device_memory_bytes": 0,
        "host_memory_bytes": 0,
    }
    assert runtime.port.observe_runtime(_command(wire)).outcome == "committed"
    assert db.query("SELECT node_state FROM host_nodes") == [{"node_state": "quarantined"}]
    stop = replace(
        COMMAND,
        request=replace(REQUEST, intent_id="runtime-stop", action="stop"),
        expected_head_version=1,
        approved_plan=None,
    )
    assert runtime.port.create_runtime_intent(stop).outcome == "committed"
    stopped = _wire(runtime, stop.request, observed="stopped")
    stopped["observed_peak_memory"] = wire["observed_peak_memory"]
    assert (
        runtime.port.observe_runtime(_command(stopped, instance_version=3)).outcome == "committed"
    )
    assert db.query(
        "SELECT quarantined_unified_physical_memory_bytes AS n,node_state FROM host_nodes"
    ) == [{"n": 7, "node_state": "quarantined"}]


def test_physical_stop_waits_for_lease_cleanup_then_returns_base_exactly_once(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    _prepare_ready(runtime)
    # Synthetic admission/quarantine facts; the release_lease port is separate.
    db.query(
        "INSERT INTO admission_leases (lease_id,operation_id,attempt_id,stage_id,"
        "host_node_id,instance_id,instance_generation,pool_id,gateway_id,fencing_"
        "token,reserved_slots,deadline_at_us,lease_state) VALUES ('lease-"
        "runtime','operation-test','attempt-test','stage-runtime','host-"
        "test','instance-new',1,'tts-realtime','gateway-"
        "test',7,1,1,'quarantined')"
    )
    db.query(
        "UPDATE engine_instances SET quarantined_slots=1,version=version+1 WHERE instance_id=%s",
        (REQUEST.instance_id,),
    )
    db.query("UPDATE resource_pools SET fencing_token=8,owner_state='reconciling_takeover'")
    db.query(
        "INSERT INTO gateway_fencing_barriers "
        "(gateway_id,pool_id,min_fencing_token,sealed_evidence_digest) VALUES "
        "('gateway-test','tts-realtime',8,%s)",
        ("sha256:" + "a" * 64,),
    )
    stop = replace(
        COMMAND,
        request=replace(REQUEST, intent_id="runtime-stop", action="stop", fencing_token=8),
        expected_head_version=1,
        approved_plan=None,
    )
    assert runtime.port.create_runtime_intent(stop).outcome == "committed"
    wire = _wire(runtime, stop.request, observed="stopped")
    host = db.query("SELECT * FROM host_nodes")
    waiting = runtime.port.observe_runtime(_command(wire, instance_version=4))
    assert (
        waiting.outcome == "committed"
        and waiting.fact is not None
        and waiting.fact.intent_state == "reconciling"
    )
    assert db.query("SELECT * FROM host_nodes") == host
    assert db.query(
        "SELECT stopped_at_us FROM engine_instances WHERE instance_id=%s", (REQUEST.instance_id,)
    ) == [{"stopped_at_us": None}]
    snapshot = runtime.snapshot()
    assert (
        runtime.port.observe_runtime(
            _command({**wire, "idempotent_replay": True}, instance_version=4)
        ).outcome
        == "replayed"
    )
    assert runtime.snapshot() == snapshot
    # Explicit synthetic completion of the independent lease-release protocol.
    db.query(
        "UPDATE admission_leases SET lease_state='released',cleanup_state='comple"
        "te',budget_released_at_us=%s,version=version+1 WHERE lease_id='lease-"
        "runtime'",
        (runtime.now(),),
    )
    db.query(
        "UPDATE engine_instances SET quarantined_slots=0,version=version+1 WHERE instance_id=%s",
        (REQUEST.instance_id,),
    )
    done = runtime.port.observe_runtime(_command(wire, instance_version=6, intent_version=2))
    assert (
        done.outcome == "committed"
        and done.fact is not None
        and done.fact.intent_state == "succeeded"
    )
    assert json.loads(done.fact.observation_document or "null") == wire
    assert db.query("SELECT held_unified_physical_memory_bytes AS n FROM host_nodes") == [{"n": 5}]
    snapshot = runtime.snapshot()
    assert (
        runtime.port.observe_runtime(_command(wire, instance_version=6, intent_version=2)).outcome
        == "replayed"
    )
    assert runtime.snapshot() == snapshot


def test_observation_failure_at_last_update_rolls_back_budget_and_versions(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    runtime.port.create_runtime_intent(COMMAND)
    db.query(
        "ALTER TABLE runtime_instance_intents ADD CONSTRAINT test_no_observation "
        "CHECK (observation_document IS NULL)"
    )
    snapshot = runtime.snapshot()
    command = _command(_wire(runtime), receipt=_receipt(runtime))
    if db.dialect == "mysql":
        with pytest.raises(pymysql.MySQLError):
            runtime.port.observe_runtime(command)
    else:
        assert runtime.port.observe_runtime(command).error_code == "reconciliation_required"
    assert runtime.snapshot() == snapshot


@pytest.mark.parametrize("same", [False, True])
def test_observers_compete_under_actual_lock_wait(
    runtime_database: RuntimeDatabase, same: bool
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    runtime.port.create_runtime_intent(COMMAND)
    wire = _wire(runtime, state="pending", observed="loading")
    other = wire if same else {**wire, "observed_instance_state": "warming_up"}
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT pool_id FROM resource_pools FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(runtime.port.observe_runtime, _command(value))
                for value in (wire, other)
            ]
            try:
                runtime.wait_for_lock(2)
            finally:
                blocker.commit()
            results = [future.result(timeout=5) for future in futures]
        assert sorted(result.outcome for result in results) == (
            ["committed", "replayed"] if same else ["committed", "conflict"]
        )
        assert db.query(
            "SELECT version FROM runtime_instance_intents WHERE intent_id=%s", (REQUEST.intent_id,)
        ) == [{"version": 2}]
        assert db.query("SELECT held_unified_physical_memory_bytes AS n FROM host_nodes") == [
            {"n": 25}
        ]
    finally:
        blocker.rollback()
        blocker.close()


def test_owner_expiry_is_checked_after_intent_lock_wait(runtime_database: RuntimeDatabase) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    runtime.port.create_runtime_intent(COMMAND)
    db.query("UPDATE resource_pools SET owner_expires_at_us=%s", (runtime.now() + 300000,))
    snapshot = runtime.snapshot()
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT intent_id FROM runtime_instance_intents WHERE intent_id=%s FOR UPDATE",
                (REQUEST.intent_id,),
            )
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                runtime.port.observe_runtime, _command(_wire(runtime), receipt=_receipt(runtime))
            )
            try:
                runtime.wait_for_lock()
                time.sleep(0.35)
            finally:
                blocker.commit()
            result = future.result(timeout=5)
        assert result.error_code == "stale_fencing_token" and runtime.snapshot() == snapshot
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_real_observation_commit_ack_loss_requires_read_only_recovery(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime = runtime_database
    runtime.port.create_runtime_intent(COMMAND)
    assert runtime.pool is not None
    pool: SpeechPool = runtime.pool

    class LostAck:
        calls = 0

        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            self.calls += 1
            with pool.connection(timeout=timeout) as connection:
                yield connection
            raise psycopg.OperationalError(
                "synthetic acknowledgement loss after observation commit"
            )

    broken = LostAck()
    result = PostgresRuntimeTransactions(cast(SpeechPool, broken)).observe_runtime(
        _command(_wire(runtime), receipt=_receipt(runtime))
    )
    assert result.outcome == "unknown" and result.recovery == "query_original" and broken.calls == 1
    snapshot = runtime.snapshot()
    queried = runtime.port.query_runtime_intent(REQUEST.identity)
    assert (
        queried.outcome == "replayed"
        and queried.fact is not None
        and queried.fact.readiness_document is not None
    )
    assert runtime.snapshot() == snapshot


def test_readiness_remains_private_to_its_owner(foundation: Foundation) -> None:
    with (
        foundation.connection("gateway") as connection,
        connection.cursor() as cursor,
        pytest.raises((psycopg.errors.InsufficientPrivilege, pymysql.OperationalError)),
    ):
        cursor.execute("SELECT readiness_document FROM runtime_instance_intents")


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_0006_failed_upgrade_preserves_rows_and_acl_then_upgrades_idempotently(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0005_runtime_intents")

    class SeedDatabase:
        def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
            sandbox.admin.execute(statement, parameters)

    seed(cast(JobDatabase, SeedDatabase()))
    role = sandbox.schema + "_reader"
    sandbox.admin.execute(f'CREATE ROLE "{role}"')
    try:
        sandbox.admin.execute(
            f"GRANT SELECT (request_document, observation_document), "
            f'UPDATE (version, updated_at_us) ON runtime_instance_intents TO "{role}"'
        )
        before = sandbox.admin.execute("SELECT * FROM runtime_instance_intents").fetchall()
        acl_sql = "SELECT relacl FROM pg_class WHERE oid='runtime_instance_intents'::regclass"
        column_sql = (
            "SELECT attname, attacl FROM pg_attribute WHERE attrelid="
            "'runtime_instance_intents'::regclass AND attnum > 0 "
            "AND NOT attisdropped AND attname <> 'readiness_document' ORDER BY attname"
        )
        acl = sandbox.admin.execute(acl_sql).fetchone()
        columns = sandbox.admin.execute(column_sql).fetchall()
        sandbox.admin.execute(
            "ALTER TABLE runtime_instance_intents ADD COLUMN readiness_document TEXT"
        )
        from sqlalchemy.exc import ProgrammingError

        with pytest.raises(ProgrammingError):
            sandbox.upgrade()
        assert sandbox.version() == "0005_runtime_intents"
        sandbox.admin.execute("ALTER TABLE runtime_instance_intents DROP COLUMN readiness_document")
        sandbox.upgrade()
        sandbox.upgrade()
        assert sandbox.version() == "0012_execution_results"
        rows = sandbox.admin.execute("SELECT * FROM runtime_instance_intents").fetchall()
        for row in rows:
            assert row.pop("readiness_document") is None
        assert rows == before
        assert sandbox.admin.execute(acl_sql).fetchone() == acl
        assert sandbox.admin.execute(column_sql).fetchall() == columns
        assert sandbox.admin.execute(
            "SELECT has_column_privilege(%s, 'runtime_instance_intents', "
            "'readiness_document', 'SELECT') AS allowed",
            (role,),
        ).fetchone() == {"allowed": False}
    finally:
        sandbox.admin.execute(f'DROP OWNED BY "{role}"')
        sandbox.admin.execute(f'DROP ROLE "{role}"')


def test_unknown_submit_blocks_base_release_even_if_a_legacy_lease_claims_released(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    _prepare_ready(runtime)
    # Explicit inconsistent legacy fixtures test fail-closed release, not the
    # separately pending release_lease or gateway termination implementations.
    db.query(
        "INSERT INTO admission_leases (lease_id,operation_id,attempt_id,stage_id,"
        "host_node_id,instance_id,instance_generation,pool_id,gateway_id,"
        "fencing_token,reserved_slots,deadline_at_us,lease_state,cleanup_state,"
        "budget_released_at_us) VALUES ('lease-runtime','operation-test',"
        "'attempt-test','stage-runtime','host-test','instance-new',1,"
        "'tts-realtime','gateway-test',7,1,1,'released','complete',1)"
    )
    db.query(
        "INSERT INTO submit_intents (submit_intent_id,lease_id,operation_id,"
        "attempt_id,command_digest,request_digest,request_document,deadline_at_us,"
        "recorded_at_us,updated_at_us,output_stream_id,sample_rate_hz,"
        "hard_output_ceiling_samples,execution_state,cleanup_state) VALUES "
        "('submit-runtime','lease-runtime','operation-test','attempt-test',%s,%s,"
        "'{}',1,1,1,'tts-runtime',24000,48000,'unknown_quarantined','quarantined')",
        ("sha256:" + "a" * 64, "sha256:" + "b" * 64),
    )
    stop = replace(
        COMMAND,
        request=replace(REQUEST, intent_id="runtime-stop", action="stop"),
        expected_head_version=1,
        approved_plan=None,
    )
    assert runtime.port.create_runtime_intent(stop).outcome == "committed"
    wire = _wire(runtime, stop.request, observed="stopped")
    waiting = runtime.port.observe_runtime(_command(wire, instance_version=3))
    assert (
        waiting.outcome == "committed"
        and waiting.fact is not None
        and waiting.fact.intent_state == "reconciling"
    )
    assert db.query("SELECT held_unified_physical_memory_bytes AS n FROM host_nodes") == [{"n": 15}]
    db.query(
        "UPDATE submit_intents SET execution_state='failed',cleanup_state='complete', "
        "version=version+1 WHERE submit_intent_id='submit-runtime'"
    )
    completed = runtime.port.observe_runtime(_command(wire, instance_version=4, intent_version=2))
    assert (
        completed.outcome == "committed"
        and completed.fact is not None
        and completed.fact.intent_state == "succeeded"
    )
    assert db.query("SELECT held_unified_physical_memory_bytes AS n FROM host_nodes") == [{"n": 5}]
