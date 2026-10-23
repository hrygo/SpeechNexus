"""Real PG/InnoDB runtime intent transactions; no GPU/controller qualification."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from tests.fake.runtime_intents import COMMAND, HOST, PLAN, REQUEST
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_jobs import database as database
from tests.support.job_database import MySQLJobTransactions
from tests.support.runtime_database import MySQLRuntimeTransactions

from speechnexus_backend.speech.application.runtime_intents import RuntimeTransactions
from speechnexus_backend.speech.domain.runtime_intents import RuntimeDeadline, RuntimeIntent
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.runtime_codec import (
    prepare_request,
    request_to_wire,
    validate_original_observation,
)
from speechnexus_backend.speech.infrastructure.runtime_postgres import PostgresRuntimeTransactions

pytestmark = pytest.mark.integration


@dataclass
class RuntimeDatabase:
    foundation: Foundation = field(repr=False)
    port: RuntimeTransactions = field(repr=False)
    pool: SpeechPool | None = field(repr=False)

    def now(self) -> int:
        expression = (
            "EXTRACT(EPOCH FROM clock_timestamp()) * 1000000"
            if self.foundation.database.dialect == "postgresql"
            else "UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000"
        )
        return int(self.foundation.database.query(f"SELECT {expression} AS now_us")[0]["now_us"])

    def wait_for_lock(self, count: int = 1) -> None:
        db = self.foundation.database
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if db.dialect == "postgresql":
                rows = db.query(
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = %s "
                    "AND cardinality(pg_blocking_pids(pid)) > 0",
                    (self.foundation.speech_role,),
                )
            else:
                rows = db.query(
                    "SELECT count(*) AS n FROM performance_schema.data_lock_waits w "
                    "JOIN performance_schema.data_locks l ON "
                    "w.REQUESTING_ENGINE_LOCK_ID = l.ENGINE_LOCK_ID WHERE l.OBJECT_SCHEMA = %s",
                    (db.schema,),
                )
            if rows[0]["n"] >= count:
                return
            time.sleep(0.01)
        pytest.fail("Runtime concurrency requires an observed real row-lock wait")

    def snapshot(self) -> dict[str, object]:
        db = self.foundation.database
        return {
            table: db.query(f"SELECT * FROM {table} ORDER BY {order}")
            for table, order in (
                ("host_nodes", "host_node_id"),
                ("engine_instances", "instance_id, instance_generation"),
                ("runtime_instance_heads", "instance_id"),
                ("runtime_instance_intents", "target_node_id, intent_id"),
                ("coordination_outbox", "event_id"),
            )
        }


@pytest.fixture
def runtime_database(foundation: Foundation) -> Iterator[RuntimeDatabase]:
    db = foundation.database
    pool: SpeechPool | None = None
    if db.dialect == "postgresql":
        pool = speech_pool(
            make_conninfo(
                db.url, user=foundation.speech_role, password="synthetic_foundation_test"
            ),
            schema=db.schema,
        )
        pool.open()
        port: RuntimeTransactions = PostgresRuntimeTransactions(pool)
    else:
        port = MySQLRuntimeTransactions(
            MySQLJobTransactions(
                db.url, db.schema, foundation.speech_role, "synthetic_foundation_test"
            )
        )
    runtime = RuntimeDatabase(foundation, port, pool)
    db.query(
        "UPDATE resource_pools SET owner_id = %s, fencing_token = 7, owner_state = 'active', "
        "owner_expires_at_us = %s",
        (COMMAND.owner_id, runtime.now() + 60000000),
    )
    assignments: list[str] = []
    parameters: tuple[object, ...] = ()
    for prefix, vector in (
        ("allocatable_", HOST.capacity),
        ("held_", HOST.held),
        ("quarantined_", HOST.quarantined),
    ):
        assignments.extend(prefix + dimension + " = %s" for dimension in DIMENSIONS)
        parameters += vector.values
    db.query("UPDATE host_nodes SET " + ", ".join(assignments), parameters)
    try:
        yield runtime
    finally:
        if pool is not None:
            pool.close()


def test_prepare_pins_full_plan_peak_and_outbox_atomically(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    before_time = runtime.now()
    result = runtime.port.create_runtime_intent(COMMAND)
    assert result.outcome == "committed" and result.fact is not None
    assert before_time <= result.fact.recorded_at_us <= runtime.now()
    assert result.fact.request == REQUEST and result.fact.approved_plan == PLAN
    host = db.query("SELECT * FROM host_nodes")[0]
    assert tuple(host["held_" + name] for name in DIMENSIONS) == HOST.held.add(PLAN.peak).values
    assert tuple(host["quarantined_" + name] for name in DIMENSIONS) == HOST.quarantined.values
    assert host["version"] == HOST.version + 1
    instance = db.query(
        "SELECT * FROM engine_instances WHERE instance_id = %s", (REQUEST.instance_id,)
    )[0]
    assert instance["instance_state"] == "preparing" and instance["base_budget_kind"] == "peak"
    assert instance["held_slots"] == instance["quarantined_slots"] == 0
    assert tuple(instance["peak_" + name] for name in DIMENSIONS) == PLAN.peak.values
    stored = db.query(
        "SELECT * FROM runtime_instance_intents WHERE intent_id = %s", (REQUEST.intent_id,)
    )[0]
    private = json.loads(stored["request_document"])
    assert (
        private["request"] == request_to_wire(REQUEST) and private["creator_id"] == COMMAND.owner_id
    )
    assert private["plan"]["qualification_digest"] == PLAN.qualification_digest
    event = db.query("SELECT * FROM coordination_outbox")[0]
    assert event["job_id"] is None and event["target_node_id"] == REQUEST.binding.target_node_id
    assert event["intent_id"] == REQUEST.intent_id and event["delivered_at_us"] is None
    assert event["event_digest"] == prepare_request(REQUEST).request_digest
    snapshot = runtime.snapshot()
    # Replay is original fact recovery even after authority/defaults have changed.
    db.query("UPDATE resource_pools SET owner_expires_at_us = 1")
    changed_defaults = replace(PLAN, qualification_digest="sha256:" + "d" * 64)
    replay = runtime.port.create_runtime_intent(replace(COMMAND, approved_plan=changed_defaults))
    assert replay.outcome == "replayed" and replay.fact == result.fact
    assert runtime.port.query_runtime_intent(REQUEST.identity).fact == result.fact
    assert runtime.snapshot() == snapshot


@pytest.mark.parametrize("change", ["deadline", "fence", "generation", "memory"])
def test_identity_cannot_be_reinterpreted(runtime_database: RuntimeDatabase, change: str) -> None:
    runtime = runtime_database
    assert runtime.port.create_runtime_intent(COMMAND).outcome == "committed"
    request = {
        "deadline": replace(REQUEST, deadline=RuntimeDeadline("2250-01-02T00:00:00Z")),
        "fence": replace(REQUEST, fencing_token=8),
        "generation": replace(REQUEST, instance_generation=2),
        "memory": replace(REQUEST, reserved_memory=replace(PLAN.memory, peak=PLAN.memory.steady)),
    }[change]
    snapshot = runtime.snapshot()
    result = runtime.port.create_runtime_intent(replace(COMMAND, request=request))
    assert result.error_code == "intent_conflict" and runtime.snapshot() == snapshot


def test_unobserved_missing_and_legacy_queries_do_not_invent_facts(
    runtime_database: RuntimeDatabase,
) -> None:
    port = runtime_database.port
    assert port.query_runtime_intent(REQUEST.identity).outcome == "unknown"
    assert port.create_runtime_intent(COMMAND).outcome == "committed"
    snapshot = runtime_database.snapshot()
    assert (
        port.query_runtime_intent(REQUEST.identity, request_digest="sha256:" + "f" * 64).error_code
        == "intent_conflict"
    )
    assert (
        port.query_runtime_intent(replace(REQUEST.identity, intent_id="runtime-test")).error_code
        == "reconciliation_required"
    )
    assert runtime_database.snapshot() == snapshot


@pytest.mark.parametrize(
    "change,code",
    [
        ("actor", "stale_fencing_token"),
        ("fence", "stale_fencing_token"),
        ("expiry", "stale_fencing_token"),
        ("reconciling", "reconciliation_required"),
        ("head", "version_conflict"),
        ("plan", "unapproved_runtime_template"),
        ("topology", "recipe_mismatch"),
        ("capacity", "capacity_exhausted"),
    ],
)
def test_failed_preconditions_leave_all_five_tables_unchanged(
    runtime_database: RuntimeDatabase,
    change: str,
    code: str,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    command = COMMAND
    if change == "actor":
        command = replace(COMMAND, owner_id="other-scheduler")
    elif change == "fence":
        command = replace(COMMAND, request=replace(REQUEST, fencing_token=6))
    elif change == "expiry":
        db.query("UPDATE resource_pools SET owner_expires_at_us = 1")
    elif change == "reconciling":
        db.query("UPDATE resource_pools SET owner_state = 'reconciling_takeover'")
    elif change == "head":
        command = replace(COMMAND, expected_head_version=1)
    elif change == "plan":
        command = replace(COMMAND, approved_plan=None)
    elif change == "topology":
        db.query("UPDATE host_nodes SET topology_digest = %s", ("sha256:" + "f" * 64,))
    else:
        db.query("UPDATE host_nodes SET allocatable_cpu_decode_millicores = 200")
    snapshot = runtime.snapshot()
    assert runtime.port.create_runtime_intent(command).error_code == code
    assert runtime.snapshot() == snapshot


def test_outbox_failure_rolls_back_budget_head_and_instance(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    # Fail the last statement using a real CHECK, after preceding writes.
    db.query(
        "ALTER TABLE coordination_outbox ADD CONSTRAINT test_no_runtime CHECK (event_kind = 'job')"
    )
    snapshot = runtime.snapshot()
    if db.dialect == "mysql":
        # The test driver reports engine errors directly; it must still roll back.
        import pymysql

        with pytest.raises(pymysql.MySQLError):
            runtime.port.create_runtime_intent(COMMAND)
    else:
        assert runtime.port.create_runtime_intent(COMMAND).error_code == "reconciliation_required"
    assert runtime.snapshot() == snapshot


def test_two_preparations_compete_for_shared_physical_budget(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    db.query("UPDATE host_nodes SET allocatable_unified_physical_memory_bytes = 32")
    other = replace(
        COMMAND, request=replace(REQUEST, intent_id="runtime-other", instance_id="instance-other")
    )
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT pool_id FROM resource_pools FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(runtime.port.create_runtime_intent, command)
                for command in (COMMAND, other)
            ]
            try:
                runtime.wait_for_lock(2)
            finally:
                blocker.commit()
            results = [future.result(timeout=5) for future in futures]
        assert sorted(result.outcome for result in results) == ["committed", "rejected"]
        assert [r.error_code for r in results if r.outcome == "rejected"] == ["capacity_exhausted"]
        assert db.query("SELECT count(*) AS n FROM coordination_outbox") == [{"n": 1}]
        assert db.query("SELECT held_unified_physical_memory_bytes AS n FROM host_nodes") == [
            {"n": 25}
        ]
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize("expired", ["owner", "deadline"])
def test_expiry_uses_database_time_after_observed_lock_wait(
    runtime_database: RuntimeDatabase, expired: str
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    command = COMMAND
    expires = runtime.now() + 300000
    if expired == "owner":
        db.query("UPDATE resource_pools SET owner_expires_at_us = %s", (expires,))
    else:
        deadline = datetime.fromtimestamp(expires / 1000000, UTC).isoformat().replace("+00:00", "Z")
        command = replace(COMMAND, request=replace(REQUEST, deadline=RuntimeDeadline(deadline)))
    snapshot = runtime.snapshot()
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT host_node_id FROM host_nodes FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(runtime.port.create_runtime_intent, command)
            try:
                runtime.wait_for_lock()
                time.sleep(0.35)
                assert runtime.now() >= expires
            finally:
                blocker.commit()
            result = future.result(timeout=5)
        assert result.error_code == (
            "stale_fencing_token" if expired == "owner" else "lease_not_active"
        )
        assert runtime.snapshot() == snapshot
    finally:
        blocker.rollback()
        blocker.close()


def _record_synthetic_terminal(
    runtime: RuntimeDatabase, request: RuntimeIntent, *, observed: str = "ready"
) -> None:
    """Seed observed facts; this is not an observe_runtime implementation."""
    value = request_to_wire(request)
    value.pop("deadline_at")
    value.update(
        intent_state="succeeded",
        observed_instance_state=observed,
        created_at="2026-10-23T00:00:00Z",
        updated_at="2026-10-23T00:00:01Z",
    )
    if request.action == "stop":
        value["stop_evidence"] = {
            "process_or_container_exited": True,
            "resources_released_confirmed": True,
            "verified_at": "2026-10-23T00:00:01Z",
        }
    document = json.dumps(value)
    validate_original_observation(document, request)
    runtime.foundation.database.query(
        "UPDATE runtime_instance_intents SET intent_state = 'succeeded', "
        "observation_document = %s, updated_at_us = %s, version = version + 1 "
        "WHERE target_node_id = %s AND intent_id = %s",
        (document, runtime.now(), request.binding.target_node_id, request.intent_id),
    )


def test_pending_or_unknown_prepare_blocks_stop_without_releasing_budget(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime = runtime_database
    assert runtime.port.create_runtime_intent(COMMAND).outcome == "committed"
    stop = replace(
        COMMAND,
        request=replace(REQUEST, intent_id="runtime-stop", action="stop"),
        expected_head_version=1,
        approved_plan=None,
    )
    for state in ("pending", "reconciling", "quarantined"):
        runtime.foundation.database.query(
            "UPDATE runtime_instance_intents SET intent_state = %s WHERE intent_id = %s",
            (state, REQUEST.intent_id),
        )
        snapshot = runtime.snapshot()
        assert runtime.port.create_runtime_intent(stop).error_code == "gateway_drain_incomplete"
        assert runtime.snapshot() == snapshot


@pytest.mark.parametrize("observed", ["stopped", "absent"])
def test_stop_keeps_budget_and_replacement_needs_verified_release(
    runtime_database: RuntimeDatabase, observed: str
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    assert runtime.port.create_runtime_intent(COMMAND).outcome == "committed"
    _record_synthetic_terminal(runtime, REQUEST)
    stop = replace(
        COMMAND,
        request=replace(REQUEST, intent_id="runtime-stop", action="stop"),
        expected_head_version=1,
        approved_plan=None,
    )
    host = db.query("SELECT * FROM host_nodes")
    assert runtime.port.create_runtime_intent(stop).outcome == "committed"
    assert db.query("SELECT * FROM host_nodes") == host
    assert db.query(
        "SELECT instance_state, base_budget_kind FROM engine_instances WHERE instance_id = %s",
        (REQUEST.instance_id,),
    ) == [{"instance_state": "draining", "base_budget_kind": "peak"}]
    replacement = replace(
        COMMAND,
        request=replace(REQUEST, intent_id="runtime-next", instance_generation=2),
        expected_head_version=2,
    )
    assert runtime.port.create_runtime_intent(replacement).error_code == "gateway_drain_incomplete"
    _record_synthetic_terminal(runtime, stop.request, observed=observed)
    assert runtime.port.create_runtime_intent(replacement).error_code == "gateway_drain_incomplete"
    # Explicit synthetic release facts stand in for separately pending ports.
    db.query(
        "UPDATE engine_instances SET instance_state = 'stopped', base_budget_kind = 'released', "
        "stopped_at_us = %s WHERE instance_id = %s",
        (runtime.now(), REQUEST.instance_id),
    )
    db.query(
        "UPDATE host_nodes SET " + ", ".join("held_" + name + " = %s" for name in DIMENSIONS),
        HOST.held.values,
    )
    assert (
        runtime.port.create_runtime_intent(replace(replacement, expected_head_version=1)).error_code
        == "version_conflict"
    )
    db.query(
        "UPDATE runtime_instance_heads SET generation_high_water = 2 WHERE instance_id = %s",
        (REQUEST.instance_id,),
    )
    assert runtime.port.create_runtime_intent(replacement).error_code == "stale_instance_generation"
    replacement = replace(replacement, request=replace(replacement.request, instance_generation=3))
    assert runtime.port.create_runtime_intent(replacement).outcome == "committed"
    assert runtime.port.query_runtime_intent(REQUEST.identity).fact is not None
    assert db.query(
        "SELECT current_generation, generation_high_water FROM runtime_instance_heads "
        "WHERE instance_id = %s",
        (REQUEST.instance_id,),
    ) == [{"current_generation": 3, "generation_high_water": 3}]
    assert db.query("SELECT held_unified_physical_memory_bytes AS n FROM host_nodes") == [{"n": 25}]


def test_quarantine_cleanup_requires_old_fence_and_sealed_barrier(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    assert runtime.port.create_runtime_intent(COMMAND).outcome == "committed"
    _record_synthetic_terminal(runtime, REQUEST)
    db.query(
        "UPDATE engine_instances SET instance_state = 'quarantined', base_budget_kind = 'q_peak', "
        "quarantined_slots = 1 WHERE instance_id = %s",
        (REQUEST.instance_id,),
    )
    assignments = ["held_" + name + " = %s" for name in DIMENSIONS]
    assignments += ["quarantined_" + name + " = %s" for name in DIMENSIONS]
    db.query(
        "UPDATE host_nodes SET " + ", ".join(assignments),
        HOST.held.values + HOST.quarantined.add(PLAN.peak).values,
    )
    db.query(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
        "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
        "reserved_slots, deadline_at_us, lease_state) VALUES "
        "('lease-runtime', 'operation-test', 'attempt-test', 'stage-runtime', 'host-test', "
        "'instance-new', 1, 'tts-realtime', 'gateway-test', 7, 1, 1, 'quarantined')"
    )
    db.query("UPDATE resource_pools SET fencing_token = 8, owner_state = 'reconciling_takeover'")
    stop = replace(
        COMMAND,
        request=replace(REQUEST, intent_id="runtime-stop", action="stop", fencing_token=8),
        expected_head_version=1,
        approved_plan=None,
    )
    snapshot = runtime.snapshot()
    assert runtime.port.create_runtime_intent(stop).error_code == "gateway_drain_incomplete"
    assert runtime.snapshot() == snapshot
    db.query(
        "INSERT INTO gateway_fencing_barriers (gateway_id, pool_id, min_fencing_token, "
        "sealed_evidence_digest) VALUES ('gateway-test', 'tts-realtime', 8, %s)",
        ("sha256:" + "e" * 64,),
    )
    db.query("UPDATE admission_leases SET fencing_token = 8 WHERE lease_id = 'lease-runtime'")
    assert runtime.port.create_runtime_intent(stop).error_code == "gateway_drain_incomplete"
    db.query("UPDATE admission_leases SET fencing_token = 7 WHERE lease_id = 'lease-runtime'")
    db.query(
        "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id, "
        "command_digest, request_digest, request_document, deadline_at_us, recorded_at_us, "
        "updated_at_us, output_stream_id, sample_rate_hz, hard_output_ceiling_samples, "
        "execution_state) VALUES ('submit-runtime', 'lease-runtime', 'operation-test', "
        "'attempt-test', %s, %s, '{}', 1, 1, 1, 'tts-runtime', 24000, 48000, 'submitting')",
        ("sha256:" + "a" * 64, "sha256:" + "b" * 64),
    )
    assert runtime.port.create_runtime_intent(stop).error_code == "gateway_drain_incomplete"
    db.query(
        "UPDATE submit_intents SET execution_state = 'unknown_quarantined' "
        "WHERE submit_intent_id = 'submit-runtime'"
    )
    host = db.query("SELECT * FROM host_nodes")
    leases = db.query("SELECT * FROM admission_leases")
    assert runtime.port.create_runtime_intent(stop).outcome == "committed"
    assert db.query("SELECT * FROM host_nodes") == host
    assert db.query("SELECT * FROM admission_leases") == leases
    assert db.query(
        "SELECT base_budget_kind, quarantined_slots FROM engine_instances WHERE instance_id = %s",
        (REQUEST.instance_id,),
    ) == [{"base_budget_kind": "q_peak", "quarantined_slots": 1}]


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_lost_real_commit_acknowledgement_only_queries_original(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime = runtime_database
    assert runtime.pool is not None
    pool: SpeechPool = runtime.pool

    class LostAcknowledgementPool:
        calls = 0

        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            self.calls += 1
            with pool.connection(timeout=timeout) as connection:
                yield connection
            raise psycopg.OperationalError("synthetic acknowledgement loss after commit")

    failing = LostAcknowledgementPool()
    result = PostgresRuntimeTransactions(cast(SpeechPool, failing)).create_runtime_intent(COMMAND)
    assert (
        result.outcome == "unknown" and result.recovery == "query_original" and failing.calls == 1
    )
    snapshot = runtime.snapshot()
    query = runtime.port.query_runtime_intent(REQUEST.identity)
    assert query.outcome == "replayed" and query.fact is not None and query.fact.request == REQUEST
    assert runtime.snapshot() == snapshot


@pytest.mark.parametrize("corruption", ["body", "digest", "plan", "event", "time"])
def test_corrupt_stored_evidence_is_not_replayed(
    runtime_database: RuntimeDatabase, corruption: str
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    assert runtime.port.create_runtime_intent(COMMAND).outcome == "committed"
    if corruption == "body":
        db.query(
            "UPDATE runtime_instance_intents SET request_document = '{}' WHERE intent_id = %s",
            (REQUEST.intent_id,),
        )
    elif corruption == "digest":
        db.query(
            "UPDATE runtime_instance_intents SET request_digest = %s WHERE intent_id = %s",
            ("sha256:" + "f" * 64, REQUEST.intent_id),
        )
    elif corruption == "plan":
        db.query(
            "UPDATE engine_instances SET peak_cpu_decode_millicores = 201 WHERE instance_id = %s",
            (REQUEST.instance_id,),
        )
    elif corruption == "event":
        db.query("UPDATE coordination_outbox SET event_digest = %s", ("sha256:" + "f" * 64,))
    else:
        db.query(
            "UPDATE runtime_instance_intents SET recorded_at_us = NULL, updated_at_us = NULL "
            "WHERE intent_id = %s",
            (REQUEST.intent_id,),
        )
    snapshot = runtime.snapshot()
    assert (
        runtime.port.query_runtime_intent(REQUEST.identity).error_code == "reconciliation_required"
    )
    assert runtime.port.create_runtime_intent(COMMAND).error_code == "reconciliation_required"
    assert runtime.snapshot() == snapshot


def test_cross_host_creation_cannot_split_one_global_generation(
    runtime_database: RuntimeDatabase,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    db.query(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state, owner_id, "
        "fencing_token, owner_state, owner_expires_at_us) VALUES "
        "('asr-realtime', 'realtime', 'active', %s, 7, 'active', %s)",
        (COMMAND.owner_id, runtime.now() + 60000000),
    )
    columns = ["host_node_id", "memory_architecture", "node_state", "topology_digest"]
    values: tuple[object, ...] = (
        "host-other",
        HOST.memory_architecture,
        "active",
        HOST.topology_digest,
    )
    for prefix, vector in (
        ("allocatable_", HOST.capacity),
        ("held_", HOST.held),
        ("quarantined_", HOST.quarantined),
    ):
        columns.extend(prefix + name for name in DIMENSIONS)
        values += vector.values
    db.query(
        "INSERT INTO host_nodes ("
        + ", ".join(columns)
        + ") VALUES ("
        + ", ".join(["%s"] * len(values))
        + ")",
        values,
    )
    binding = replace(REQUEST.binding, target_node_id="host-other", pool_id="asr-realtime")
    other = replace(
        COMMAND,
        request=replace(REQUEST, binding=binding, intent_id="runtime-other"),
        approved_plan=replace(PLAN, binding=binding),
    )
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT host_node_id FROM host_nodes ORDER BY host_node_id FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(runtime.port.create_runtime_intent, command)
                for command in (COMMAND, other)
            ]
            try:
                runtime.wait_for_lock(2)
            finally:
                blocker.commit()
            results = [future.result(timeout=5) for future in futures]
        assert sorted(result.outcome for result in results) == ["committed", "conflict"]
        assert db.query(
            "SELECT count(*) AS n FROM engine_instances WHERE instance_id = %s",
            (REQUEST.instance_id,),
        ) == [{"n": 1}]
        assert db.query("SELECT count(*) AS n FROM coordination_outbox") == [{"n": 1}]
        assert (
            sum(
                row["held_unified_physical_memory_bytes"]
                for row in db.query("SELECT * FROM host_nodes")
            )
            == 30
        )
    finally:
        blocker.rollback()
        blocker.close()
