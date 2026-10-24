"""Real PG/InnoDB complete admission; synthetic qualification, no GPU claim."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from speechnexus_contracts.validation import validate_coordination
from tests.fake.admission import ADMISSION, APPROVED, BUSINESS, COMMAND
from tests.fake.runtime_intents import COMMAND as RUNTIME_COMMAND
from tests.fake.runtime_intents import PLAN
from tests.fake.runtime_intents import REQUEST as RUNTIME_REQUEST
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_observation import observed_request
from tests.integration.test_gateway_stop_evidence import evidence_for, new_pool
from tests.integration.test_gateway_submit_ledger import identity_for
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import RuntimeDatabase
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_runtime_observations import _command, _prepare_ready, _receipt, _wire
from tests.support.admission_database import MySQLAdmissionTransactions
from tests.support.gateway_database import MySQLSubmissionLedger
from tests.support.job_database import MySQLJobTransactions
from tests.support.lease_database import MySQLLeaseTransactions

from speechnexus_backend.speech.application.admission import AdmissionService, AdmissionTransactions
from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmitRecipe,
    ApprovedStage,
    StageAllocation,
)
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import QuarantineLease
from speechnexus_backend.speech.domain.owners import OwnerAuthority
from speechnexus_backend.speech.infrastructure.admission_codec import (
    business_command_from_value,
    prepare_admission,
    request_from_wire,
    request_to_wire,
)
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.jobs_sql import create_in_transaction
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.interfaces.admission_transactions import (
    AdmissionTransactionBoundary,
)
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.recovery import MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionState,
    RecordStop,
    StopEvidenceKind,
    SubmissionIntent,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.infrastructure.submit_sql import from_unix_micros

pytestmark = pytest.mark.integration


@dataclass
class AdmissionDatabase:
    runtime: RuntimeDatabase = field(repr=False)
    port: AdmissionTransactions = field(repr=False)
    command: AdmitRecipe = field(repr=False)

    def snapshot(self) -> dict[str, object]:
        db = self.runtime.foundation.database
        return {
            table: db.query(f"SELECT * FROM {table} ORDER BY {order}")
            for table, order in (
                ("workspace_quotas", "workspace_id"),
                ("host_nodes", "host_node_id"),
                ("engine_instances", "instance_id,instance_generation"),
                ("admission_operations", "operation_id"),
                ("admission_attempts", "operation_id,attempt_id"),
                ("admission_requests", "operation_id,attempt_id"),
                ("admission_leases", "lease_id"),
                ("coordination_jobs", "job_id"),
                ("coordination_outbox", "event_id"),
            )
        }


@pytest.fixture
def admission_database(runtime_database: RuntimeDatabase) -> Iterator[AdmissionDatabase]:
    runtime = runtime_database
    _prepare_ready(runtime)
    foundation = runtime.foundation
    db = foundation.database
    if db.dialect == "postgresql":
        assert runtime.pool is not None
        port: AdmissionTransactions = PostgresAdmissionTransactions(
            runtime.pool, max_command_bytes=1048576
        )
    else:
        port = MySQLAdmissionTransactions(
            MySQLJobTransactions(
                db.url, db.schema, foundation.speech_role, "synthetic_foundation_test"
            ),
            max_command_bytes=1048576,
        )
    now = runtime.now()
    command = replace(
        COMMAND,
        request=replace(ADMISSION, deadline_at_us=now + 60000000),
        approved_recipe=replace(APPROVED, admission_expires_at_us=now + 60000000),
    )
    yield AdmissionDatabase(runtime, port, command)


def test_ready_runtime_to_atomic_recipe_job_and_original_query(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    before = admission.snapshot()
    result = admission.port.admit_recipe(admission.command)
    assert result.outcome == "committed" and result.fact is not None, result
    assert result.fact.reservations[0].physical == PLAN.per_slot
    after = admission.snapshot()
    assert db.counts() == (1, 2)  # One runtime event and one new Job event.
    host = db.query("SELECT * FROM host_nodes")[0]
    old_host = before["host_nodes"]
    assert isinstance(old_host, list)
    for name, increment in zip(DIMENSIONS, PLAN.per_slot.values, strict=True):
        assert host["held_" + name] == old_host[0]["held_" + name] + increment
        assert host["quarantined_" + name] == old_host[0]["quarantined_" + name]
    prepared = prepare_admission(admission.command, max_command_bytes=1048576)
    assert db.query("SELECT request_digest FROM coordination_jobs") == [
        {"request_digest": BUSINESS.request_digest}
    ]
    assert db.query("SELECT request_digest FROM admission_requests") == [
        {"request_digest": prepared.request_digest}
    ]
    assert admission.port.admit_recipe(admission.command).outcome == "replayed"
    assert admission.port.query_admission(admission.command.request.identity).fact == result.fact
    assert admission.snapshot() == after


def test_original_replay_does_not_consult_new_policy_or_owner(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    result = admission.port.admit_recipe(admission.command)
    assert result.outcome == "committed"
    db.query("UPDATE workspace_quotas SET policy_revision = 'other-policy'")
    db.query("UPDATE resource_pools SET owner_expires_at_us = 1, pool_state = 'disabled'")
    before = admission.snapshot()
    replay = admission.port.admit_recipe(admission.command)
    assert replay.outcome == "replayed" and replay.fact == result.fact
    assert admission.snapshot() == before


def _fresh(command: AdmitRecipe, suffix: str, *, operation_id: str | None = None) -> AdmitRecipe:
    request = command.request
    identity = AdmissionIdentity(
        operation_id or request.identity.operation_id + suffix, request.identity.attempt_id + suffix
    )
    job = replace(
        request.job,
        job_id=request.job.job_id + suffix,
        event_id=request.job.event_id + suffix,
        operation_id=identity.operation_id,
        attempt_id=identity.attempt_id,
    )
    return replace(
        command,
        request=replace(
            request,
            identity=identity,
            job=job,
            stages=tuple(replace(s, lease_id=s.lease_id + suffix) for s in request.stages),
        ),
    )


def _second_runtime(admission: AdmissionDatabase) -> tuple[AdmitRecipe, AdmitRecipe]:
    runtime = admission.runtime
    db = runtime.foundation.database
    db.query(
        "INSERT INTO resource_pools (pool_id,service_class,pool_state,owner_id,fencing_token, "
        "owner_state,owner_expires_at_us) "
        "VALUES ('asr-realtime','realtime','active',%s,7,'active',%s)",
        (RUNTIME_COMMAND.owner_id, runtime.now() + 60000000),
    )
    plan = replace(
        PLAN, binding=replace(PLAN.binding, pool_id="asr-realtime", gateway_id="gateway-asr")
    )
    request = replace(
        RUNTIME_REQUEST, intent_id="runtime-asr", instance_id="instance-asr", binding=plan.binding
    )
    assert (
        runtime.port.create_runtime_intent(
            replace(RUNTIME_COMMAND, request=request, approved_plan=plan)
        ).outcome
        == "committed"
    )
    wire = _wire(runtime, request)
    assert (
        runtime.port.observe_runtime(_command(wire, receipt=_receipt(runtime, request))).outcome
        == "committed"
    )
    stage = StageAllocation(
        "stage-review",
        "lease-admit-review",
        "asr-realtime",
        "host-test",
        "gateway-asr",
        "instance-asr",
        1,
        2,
        1,
    )
    authority = OwnerAuthority("asr-realtime", RUNTIME_COMMAND.owner_id, 7)
    original = admission.command
    combined = replace(
        original,
        request=replace(
            original.request,
            stages=(*original.request.stages, stage),
            authorities=(*original.request.authorities, authority),
        ),
        approved_recipe=replace(
            original.approved_recipe,
            stages=(*original.approved_recipe.stages, ApprovedStage(stage.stage_id, 1, plan)),
        ),
    )
    other = _fresh(original, "-other")
    other = replace(
        other,
        request=replace(
            other.request,
            stages=(replace(stage, lease_id="lease-admit-other"),),
            authorities=(authority,),
        ),
        approved_recipe=replace(
            other.approved_recipe, stages=(ApprovedStage(stage.stage_id, 1, plan),)
        ),
    )
    return combined, other


def _begin(connection: Any, dialect: str) -> None:
    if dialect == "postgresql":
        connection.autocommit = False
    else:
        connection.autocommit(False)


def test_two_required_pools_commit_one_attempt_and_all_stage_resources(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    command, _ = _second_runtime(admission)
    db = admission.runtime.foundation.database
    before = db.query("SELECT * FROM host_nodes")[0]
    result = admission.port.admit_recipe(command)
    assert result.outcome == "committed" and result.fact is not None, result
    assert len(result.fact.reservations) == 2
    assert db.query("SELECT held_attempts,version FROM workspace_quotas") == [
        {"held_attempts": 2, "version": 2}
    ]
    after = db.query("SELECT * FROM host_nodes")[0]
    for name, extra in zip(DIMENSIONS, PLAN.per_slot.scaled(2).values, strict=True):
        assert after["held_" + name] == before["held_" + name] + extra
    assert admission.port.query_admission(command.request.identity).fact == result.fact


@pytest.mark.parametrize("failure", ["capacity", "second-lease", "outbox"])
def test_complete_recipe_rolls_back_every_record_and_projection(
    admission_database: AdmissionDatabase, failure: str
) -> None:
    admission = admission_database
    command, _ = _second_runtime(admission)
    db = admission.runtime.foundation.database
    if failure == "capacity":
        host = db.query("SELECT * FROM host_nodes")[0]
        db.query(
            "UPDATE host_nodes SET allocatable_unified_physical_memory_bytes = %s",
            (
                host["held_unified_physical_memory_bytes"]
                + host["quarantined_unified_physical_memory_bytes"]
                + PLAN.per_slot.unified_physical_memory_bytes,
            ),
        )
    elif failure == "second-lease":
        db.query(
            "ALTER TABLE admission_leases ADD CONSTRAINT synthetic_second_stage_failure "
            "CHECK (stage_id <> 'stage-review')"
        )
    else:
        db.query(
            "ALTER TABLE coordination_outbox ADD CONSTRAINT synthetic_outbox_failure "
            "CHECK (event_id <> 'event-admit')"
        )
    before = admission.snapshot()
    result = admission.port.admit_recipe(command)
    assert result.outcome == "rejected", result
    assert result.error_code == (
        "capacity_exhausted" if failure == "capacity" else "reconciliation_required"
    )
    assert admission.snapshot() == before


def test_concurrent_identical_requests_replay_after_real_parent_wait(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    foundation = admission.runtime.foundation
    with foundation.connection("speech") as blocker, ThreadPoolExecutor(max_workers=2) as executor:
        _begin(blocker, foundation.database.dialect)
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT pool_id FROM resource_pools WHERE pool_id='tts-realtime' FOR UPDATE"
            )
        futures = [
            executor.submit(admission.port.admit_recipe, admission.command) for _ in range(2)
        ]
        try:
            admission.runtime.wait_for_lock(count=2)
        finally:
            blocker.commit()
        results = [future.result(timeout=5) for future in futures]
    assert sorted(result.outcome for result in results) == ["committed", "replayed"]
    assert results[0].fact == results[1].fact
    assert foundation.database.query("SELECT count(*) AS n FROM admission_operations") == [{"n": 1}]
    assert foundation.database.query("SELECT held_attempts FROM workspace_quotas") == [
        {"held_attempts": 2}
    ]


def test_different_pools_compete_for_the_same_host_last_increment(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    _, other = _second_runtime(admission)
    foundation = admission.runtime.foundation
    db = foundation.database
    db.query(
        "INSERT INTO workspace_quotas (workspace_id,max_attempts,policy_revision) "
        "VALUES ('workspace-other',2,'policy-test')"
    )
    other = replace(other, request=replace(other.request, workspace_id="workspace-other"))
    host = db.query("SELECT * FROM host_nodes")[0]
    db.query(
        "UPDATE host_nodes SET allocatable_unified_physical_memory_bytes = %s",
        (
            host["held_unified_physical_memory_bytes"]
            + host["quarantined_unified_physical_memory_bytes"]
            + PLAN.per_slot.unified_physical_memory_bytes,
        ),
    )
    with foundation.connection("speech") as blocker, ThreadPoolExecutor(max_workers=2) as executor:
        _begin(blocker, db.dialect)
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT host_node_id FROM host_nodes WHERE host_node_id='host-test' FOR UPDATE"
            )
        futures = [
            executor.submit(admission.port.admit_recipe, command)
            for command in (admission.command, other)
        ]
        try:
            admission.runtime.wait_for_lock(count=2)
        finally:
            blocker.commit()
        results = [future.result(timeout=5) for future in futures]
    assert sorted(result.outcome for result in results) == ["committed", "rejected"]
    assert [result.error_code for result in results if result.outcome == "rejected"] == [
        "capacity_exhausted"
    ]
    assert db.query("SELECT count(*) AS n FROM admission_operations") == [{"n": 1}]


def test_new_attempt_cannot_bypass_original_operation(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    assert admission.port.admit_recipe(admission.command).outcome == "committed"
    before = admission.snapshot()
    next_attempt = _fresh(
        admission.command, "-next", operation_id=admission.command.request.identity.operation_id
    )
    result = admission.port.admit_recipe(next_attempt)
    assert result.error_code == "reconciliation_required"
    assert admission.snapshot() == before


@pytest.mark.parametrize("change", ["business", "deadline", "lease", "quota-version"])
def test_same_identity_different_complete_intent_is_conflict(
    admission_database: AdmissionDatabase, change: str
) -> None:
    admission = admission_database
    assert admission.port.admit_recipe(admission.command).outcome == "committed"
    command = admission.command
    request = command.request
    if change == "business":
        business = business_command_from_value(
            {"synthetic_effective_command": False}, max_command_bytes=1048576
        )
        command = replace(
            command,
            business_command=business,
            request=replace(
                request, job=replace(request.job, request_digest=business.request_digest)
            ),
        )
    elif change == "deadline":
        command = replace(
            command, request=replace(request, deadline_at_us=request.deadline_at_us + 1)
        )
    elif change == "lease":
        command = replace(
            command,
            request=replace(request, stages=(replace(request.stages[0], lease_id="other-lease"),)),
        )
    else:
        command = replace(command, request=replace(request, expected_quota_version=2))
    before = admission.snapshot()
    assert admission.port.admit_recipe(command).error_code == "idempotency_conflict"
    assert admission.snapshot() == before


def test_missing_and_legacy_queries_are_read_only_without_body_backfill(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    before = admission.snapshot()
    assert admission.port.query_admission(admission.command.request.identity).outcome == "unknown"
    legacy = AdmissionIdentity("operation-test", "attempt-test")
    assert admission.port.query_admission(legacy).error_code == "reconciliation_required"
    assert admission.snapshot() == before


@pytest.mark.parametrize("damage", ["stage-set", "request-digest", "missing-request", "per-slot"])
def test_observed_inconsistent_records_fail_closed_without_repairs(
    admission_database: AdmissionDatabase, damage: str
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    if damage == "per-slot":
        db.query(
            "UPDATE engine_instances SET per_slot_unified_physical_memory_bytes=0 "
            "WHERE instance_id='instance-new'"
        )
    else:
        assert admission.port.admit_recipe(admission.command).outcome == "committed"
        if damage == "stage-set":
            db.query(
                "UPDATE admission_attempts SET required_stage_ids = %s WHERE operation_id=%s",
                (
                    json.dumps(["stage-main", "missing-stage"]),
                    admission.command.request.identity.operation_id,
                ),
            )
        elif damage == "request-digest":
            db.query("UPDATE admission_requests SET request_digest = %s", ("sha256:" + "a" * 64,))
        else:
            db.query(
                "DELETE FROM admission_requests"
            )  # Isolated synthetic corruption; runtime roles cannot delete it.
    before = admission.snapshot()
    result = (
        admission.port.admit_recipe(admission.command)
        if damage == "per-slot"
        else admission.port.query_admission(admission.command.request.identity)
    )
    assert result.error_code == "reconciliation_required", result
    assert admission.snapshot() == before


def test_preexisting_standalone_job_is_not_adopted_by_later_admission(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    assert db.port.create_job(admission.command.request.job).outcome == "committed"
    before = admission.snapshot()
    assert admission.port.admit_recipe(admission.command).error_code == "reconciliation_required"
    assert admission.snapshot() == before


def test_unsupported_quota_ledger_rolls_back_the_operation_anchor(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    command = replace(
        admission.command,
        approved_recipe=replace(
            admission.command.approved_recipe,
            quota_dimensions=frozenset({"concurrent_attempts", "generated_samples"}),
        ),
    )
    before = admission.snapshot()
    assert admission.port.admit_recipe(command).error_code == "policy_or_resource_disabled"
    assert admission.snapshot() == before


def _wait_for_outbox_unique_wait(admission: AdmissionDatabase) -> None:
    foundation = admission.runtime.foundation
    db = foundation.database
    end = time.monotonic() + 2
    while time.monotonic() < end:
        if db.dialect == "postgresql":
            pending = db.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                "AND cardinality(pg_blocking_pids(pid))>0 "
                "AND query LIKE 'INSERT INTO coordination_outbox%%'",
                (foundation.speech_role,),
            )
        else:
            pending = db.query(
                "SELECT count(*) AS n FROM performance_schema.data_lock_waits w "
                "JOIN performance_schema.data_locks l "
                "ON w.REQUESTING_ENGINE_LOCK_ID=l.ENGINE_LOCK_ID "
                "WHERE l.OBJECT_SCHEMA=%s AND l.OBJECT_NAME='coordination_outbox'",
                (db.schema,),
            )
        if pending[0]["n"]:
            return
        time.sleep(0.01)
    pytest.fail("Admission did not reach an observed final outbox unique-key wait")


@pytest.mark.parametrize("expires", ["request", "owner", "readiness", "approval"])
def test_last_outbox_unique_wait_rechecks_all_authority_windows(
    admission_database: AdmissionDatabase, expires: str
) -> None:
    admission = admission_database
    foundation = admission.runtime.foundation
    db = foundation.database
    short_deadline = admission.runtime.now() + 600000
    command = admission.command
    code = "lease_not_active"
    if expires == "request":
        command = replace(command, request=replace(command.request, deadline_at_us=short_deadline))
    elif expires == "owner":
        db.query("UPDATE resource_pools SET owner_expires_at_us=%s", (short_deadline,))
        code = "stale_fencing_token"
    elif expires == "approval":
        command = replace(
            command,
            approved_recipe=replace(
                command.approved_recipe, admission_expires_at_us=short_deadline
            ),
        )
        code = "policy_or_resource_disabled"
    else:
        original = db.query(
            "SELECT readiness_document FROM runtime_instance_intents WHERE intent_id='runtime-new'"
        )[0]["readiness_document"]
        receipt = json.loads(original)
        receipt["expires_at_us"] = short_deadline
        db.query(
            "UPDATE runtime_instance_intents SET readiness_document=%s "
            "WHERE intent_id='runtime-new'",
            (json.dumps(receipt, separators=(",", ":")),),
        )
        code = "reconciliation_required"
    before = admission.snapshot()
    with foundation.connection("speech") as blocker, ThreadPoolExecutor(max_workers=1) as executor:
        _begin(blocker, db.dialect)
        with blocker.cursor() as cursor:
            unrelated = replace(
                command.request.job,
                job_id="job-wait-blocker",
                operation_id="operation-wait-blocker",
                attempt_id="attempt-wait-blocker",
                event_id="event-wait-blocker",
            )
            assert create_in_transaction(cursor, unrelated).outcome == "committed"
            # Test-only pending row holds the final unique key; it is rolled
            # back, so the application insertion succeeds after the real wait.
            cursor.execute(
                "INSERT INTO coordination_outbox (event_id,job_id,event_digest) VALUES (%s,%s,%s)",
                (command.request.job.event_id, unrelated.job_id, unrelated.event_digest),
            )
        future = executor.submit(admission.port.admit_recipe, command)
        try:
            _wait_for_outbox_unique_wait(admission)
            while admission.runtime.now() <= short_deadline:
                time.sleep(0.01)
        finally:
            blocker.rollback()
        result = future.result(timeout=5)
    assert result.error_code == code, result
    assert admission.snapshot() == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("error_type", [psycopg.OperationalError, psycopg.errors.QueryCanceled])
def test_actual_commit_then_lost_ack_queries_original_without_new_resources(
    admission_database: AdmissionDatabase,
    error_type: type[psycopg.Error],
) -> None:
    admission = admission_database
    assert admission.runtime.pool is not None

    class CommitLostPool:
        def __init__(self, pool: SpeechPool) -> None:
            self.pool = pool
            self.armed = True

        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            with self.pool.connection(timeout=timeout) as connection:
                yield connection
            if self.armed:
                self.armed = False
                raise error_type("synthetic acknowledgement loss after actual commit")

    lost = CommitLostPool(admission.runtime.pool)
    port = PostgresAdmissionTransactions(cast(SpeechPool, lost), max_command_bytes=1048576)
    result = port.admit_recipe(admission.command)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    committed = admission.snapshot()
    assert committed["admission_operations"]
    bypass = _fresh(
        admission.command,
        "-after-unknown",
        operation_id=admission.command.request.identity.operation_id,
    )
    assert port.admit_recipe(bypass).error_code == "reconciliation_required"
    assert admission.snapshot() == committed
    recovered = port.query_admission(admission.command.request.identity)
    assert recovered.outcome == "replayed" and recovered.fact is not None
    assert port.admit_recipe(admission.command).fact == recovered.fact
    assert admission.snapshot() == committed


def _discrete_runtime(admission: AdmissionDatabase) -> AdmitRecipe:
    runtime = admission.runtime
    db = runtime.foundation.database
    plan = replace(
        PLAN,
        memory_architecture="discrete_device_memory",
        service_class="batch",
        binding=replace(
            PLAN.binding,
            target_node_id="host-discrete",
            pool_id="asr-batch",
            gateway_id="gateway-discrete",
        ),
        peak=PhysicalResources(0, 20, 10, 200, 10, 50),
        steady=PhysicalResources(0, 10, 5, 100, 5, 25),
        per_slot=PhysicalResources(0, 3, 2, 20, 2, 5),
    )
    db.query(
        "INSERT INTO host_nodes (host_node_id,memory_architecture,node_state,topology_digest, "
        "allocatable_device_memory_bytes,allocatable_host_memory_bytes, "
        "allocatable_cpu_decode_millicores,allocatable_host_pcm_buffer_bytes, "
        "allocatable_scratch_disk_bytes) "
        "VALUES ('host-discrete','discrete_device_memory','active',%s,100,100,2000,100,1000)",
        (plan.topology_digest,),
    )
    db.query(
        "INSERT INTO resource_pools (pool_id,service_class,pool_state,owner_id,fencing_token, "
        "owner_state,owner_expires_at_us) VALUES ('asr-batch','batch','active',%s,7,'active',%s)",
        (RUNTIME_COMMAND.owner_id, runtime.now() + 60000000),
    )
    request = replace(
        RUNTIME_REQUEST,
        intent_id="runtime-discrete",
        instance_id="instance-discrete",
        binding=plan.binding,
        reserved_memory=plan.memory,
    )
    assert (
        runtime.port.create_runtime_intent(
            replace(RUNTIME_COMMAND, request=request, approved_plan=plan)
        ).outcome
        == "committed"
    )
    wire = _wire(runtime, request)
    assert (
        runtime.port.observe_runtime(_command(wire, receipt=_receipt(runtime, request))).outcome
        == "committed"
    )
    db.query(
        "INSERT INTO workspace_quotas (workspace_id,max_attempts,policy_revision) "
        "VALUES ('workspace-discrete',2,'policy-test')"
    )
    command = _fresh(admission.command, "-discrete")
    stage = StageAllocation(
        "stage-main",
        "lease-discrete",
        "asr-batch",
        "host-discrete",
        "gateway-discrete",
        "instance-discrete",
        1,
        2,
        1,
    )
    return replace(
        command,
        request=replace(
            command.request,
            workspace_id="workspace-discrete",
            service_class="batch",
            stages=(stage,),
            authorities=(OwnerAuthority("asr-batch", RUNTIME_COMMAND.owner_id, 7),),
        ),
        approved_recipe=replace(
            command.approved_recipe,
            service_class="batch",
            stages=(ApprovedStage("stage-main", 1, plan),),
        ),
    )


def test_discrete_parent_admission_charges_device_host_and_auxiliary_resources(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    command = _discrete_runtime(admission)
    command = replace(
        command,
        request=replace(
            command.request, stages=(replace(command.request.stages[0], reserved_slots=2),)
        ),
    )
    db = admission.runtime.foundation.database
    before = db.query("SELECT * FROM host_nodes WHERE host_node_id='host-discrete'")[0]
    result = admission.port.admit_recipe(command)
    assert result.outcome == "committed" and result.fact is not None, result
    assert result.fact.reservations[0].physical == PhysicalResources(0, 6, 4, 40, 4, 10)
    after = db.query("SELECT * FROM host_nodes WHERE host_node_id='host-discrete'")[0]
    for name, extra in zip(DIMENSIONS, result.fact.reservations[0].physical.values, strict=True):
        assert after["held_" + name] == before["held_" + name] + extra
    assert after["held_unified_physical_memory_bytes"] == 0


@pytest.mark.parametrize("dimension", [1, 2, 3, 4, 5])
def test_discrete_full_recipe_refuses_each_exhausted_parent_dimension(
    admission_database: AdmissionDatabase, dimension: int
) -> None:
    admission = admission_database
    command = _discrete_runtime(admission)
    db = admission.runtime.foundation.database
    name = DIMENSIONS[dimension]
    row = db.query("SELECT * FROM host_nodes WHERE host_node_id='host-discrete'")[0]
    extra = command.approved_recipe.stages[0].runtime_plan.per_slot.values[dimension]
    db.query(
        f"UPDATE host_nodes SET allocatable_{name}=%s WHERE host_node_id='host-discrete'",
        (row["held_" + name] + extra - 1,),
    )
    before = admission.snapshot()
    assert admission.port.admit_recipe(command).error_code == "capacity_exhausted"
    assert admission.snapshot() == before


def test_frozen_boundary_returns_only_original_admission_projection(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    boundary = AdmissionTransactionBoundary(
        AdmissionService(admission.port), decode_request=request_from_wire
    )
    result = boundary.admit_recipe(
        request_to_wire(admission.command.request),
        business_command=admission.command.business_command,
        approved_recipe=admission.command.approved_recipe,
    )
    assert result["outcome"] == "committed", result
    serialized = json.dumps(result)
    assert "synthetic_effective_command" not in serialized
    assert "host_node_id" not in serialized and "gateway_id" not in serialized
    prepared = prepare_admission(admission.command, max_command_bytes=1048576)
    queried = boundary.query_outcome(
        {
            "target": {
                "kind": "attempt",
                "identity": {
                    "operation_id": admission.command.request.identity.operation_id,
                    "attempt_id": admission.command.request.identity.attempt_id,
                },
            },
            "request_digest": prepared.request_digest,
        }
    )
    assert queried["outcome"] == "replayed" and queried["value"] == result["value"]


@pytest.mark.parametrize("mode", ["no-dispatch", "success", "unknown"])
def test_native_admission_to_gateway_fact_and_last_stage_release(
    admission_database: AdmissionDatabase, mode: str
) -> None:
    admission = admission_database
    command, _ = _second_runtime(admission)
    foundation = admission.runtime.foundation
    db = foundation.database
    baseline = db.query("SELECT * FROM host_nodes")[0]
    result = admission.port.admit_recipe(command)
    assert result.outcome == "committed" and result.fact is not None, result
    stage = next(stage for stage in command.request.stages if stage.stage_id == "stage-main")
    examples = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures/coordination/transaction-examples.json"
        ).read_text()
    )
    full_command = next(
        c["request"]["execution_request"]["command"]
        for c in examples["valid_commands"]
        if c["operation"] == "create_submit"
        and c["request"]["execution_request"]["command"]["kind"] == "tts"
    )
    identity = replace(
        identity_for(
            stage.lease_id,
            submit_intent_id="submit-admission",
            attempt_id=command.request.identity.attempt_id,
            stage_id=stage.stage_id,
            fencing_token=7,
        ),
        operation_id=command.request.identity.operation_id,
        instance_id=stage.instance_id,
        command_digest=command_digest(full_command),
        deadline_at=from_unix_micros(command.request.deadline_at_us),
    )
    request = SubmitRequest(identity, full_command, output_stream_id="tts-admission")
    validate_coordination("create_submit", request.effective_request)
    at = from_unix_micros(admission.runtime.now())
    intent = SubmissionIntent(
        identity, at, at, request.request_digest, output_stream_id=request.output_stream_id
    )
    pool = new_pool(foundation) if db.dialect == "postgresql" else None
    try:
        ledger = (
            PostgresSubmissionLedger(pool)
            if pool is not None
            else MySQLSubmissionLedger(foundation)
        )
        assert admission.runtime.pool is not None or db.dialect == "mysql"
        leases = (
            PostgresLeaseTransactions(admission.runtime.pool)
            if admission.runtime.pool is not None
            else MySQLLeaseTransactions(
                MySQLJobTransactions(
                    db.url, db.schema, foundation.speech_role, "synthetic_foundation_test"
                )
            )
        )
        ledger.add(intent, request_document=request.request_document)
        if mode == "no-dispatch":
            ledger.record_execution_observation(observed_request(intent, unsent=True))
        else:
            ledger.record_execution_observation(observed_request(intent))
            if mode == "unknown":
                ledger.mark_execution_unknown(
                    MarkExecutionUnknown(
                        identity.submit_intent_id,
                        ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
                        admission.runtime.now(),
                    )
                )
                assert (
                    leases.quarantine_lease(
                        QuarantineLease(
                            stage.lease_id, "engine_transport_unknown", admission.runtime.now()
                        )
                    ).outcome
                    == "committed"
                )
                current = leases.query_lease(stage.lease_id)
                assert current.fact is not None
                before_stop = admission.snapshot()
                assert (
                    leases.release_lease(
                        ReleaseLease(stage.lease_id, current.fact.version)
                    ).error_code
                    == "execution_cleanup_pending"
                )
                assert admission.snapshot() == before_stop
            ledger.record_stop(
                RecordStop(
                    evidence_for(
                        foundation,
                        submit_intent_id=identity.submit_intent_id,
                        evidence_kind=StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER
                        if mode == "unknown"
                        else StopEvidenceKind.ENGINE_TERMINAL_RESPONSE,
                    ),
                    ExecutionState.ABORTED if mode == "unknown" else ExecutionState.SUCCEEDED,
                )
            )
        gateway = {
            table: db.query(f"SELECT * FROM {table}")
            for table in ("submit_intents", "execution_stop_evidence", "output_sample_intervals")
        }
        for index, stage_to_release in enumerate(
            sorted(command.request.stages, key=lambda s: s.stage_id)
        ):
            current = leases.query_lease(stage_to_release.lease_id)
            assert current.fact is not None
            assert (
                leases.release_lease(
                    ReleaseLease(stage_to_release.lease_id, current.fact.version)
                ).outcome
                == "committed"
            )
            quota = db.query("SELECT held_attempts,quarantined_attempts FROM workspace_quotas")[0]
            if index == 0:
                assert quota == (
                    {"held_attempts": 1, "quarantined_attempts": 1}
                    if mode == "unknown"
                    else {"held_attempts": 2, "quarantined_attempts": 0}
                )
        assert db.query("SELECT held_attempts,quarantined_attempts FROM workspace_quotas") == [
            {"held_attempts": 1, "quarantined_attempts": 0}
        ]
        after = db.query("SELECT * FROM host_nodes")[0]
        for name in DIMENSIONS:
            assert after["held_" + name] == baseline["held_" + name]
            assert after["quarantined_" + name] == baseline["quarantined_" + name]
        assert {table: db.query(f"SELECT * FROM {table}") for table in gateway} == gateway
        recovered = admission.port.query_admission(command.request.identity)
        assert (
            recovered.fact is not None and recovered.fact.reservations == result.fact.reservations
        )
    finally:
        if pool is not None:
            pool.close()


def test_global_operation_key_serializes_disjoint_workspaces_pools_and_hosts(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    other = _discrete_runtime(admission)
    shared_id = admission.command.request.identity.operation_id
    other = replace(
        other,
        request=replace(
            other.request,
            identity=replace(other.request.identity, operation_id=shared_id),
            job=replace(other.request.job, operation_id=shared_id),
        ),
    )
    foundation = admission.runtime.foundation
    db = foundation.database

    def await_operation_waits() -> None:
        until = time.monotonic() + 2
        while time.monotonic() < until:
            if db.dialect == "postgresql":
                waiting = db.query(
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                    "AND cardinality(pg_blocking_pids(pid))>0 "
                    "AND query LIKE '%%admission_operations%%'",
                    (foundation.speech_role,),
                )
            else:
                waiting = db.query(
                    "SELECT count(*) AS n FROM performance_schema.data_lock_waits w "
                    "JOIN performance_schema.data_locks l "
                    "ON w.REQUESTING_ENGINE_LOCK_ID=l.ENGINE_LOCK_ID "
                    "WHERE l.OBJECT_SCHEMA=%s AND l.OBJECT_NAME='admission_operations'",
                    (db.schema,),
                )
            if waiting[0]["n"] >= 2:
                return
            time.sleep(0.01)
        pytest.fail("Both disjoint-resource transactions must wait on the same operation authority")

    with foundation.connection("speech") as blocker, ThreadPoolExecutor(max_workers=2) as executor:
        _begin(blocker, db.dialect)
        with blocker.cursor() as cursor:
            cursor.execute(
                "INSERT INTO admission_operations (operation_id,workspace_id,request_digest, "
                "request_document,initial_attempt_id,current_attempt_id,recorded_at_us) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    shared_id,
                    admission.command.request.workspace_id,
                    BUSINESS.request_digest,
                    BUSINESS.document,
                    admission.command.request.identity.attempt_id,
                    admission.command.request.identity.attempt_id,
                    admission.runtime.now(),
                ),
            )
        futures = [
            executor.submit(admission.port.admit_recipe, command)
            for command in (admission.command, other)
        ]
        try:
            await_operation_waits()
        finally:
            blocker.rollback()  # Only the native contenders may create a committed anchor.
        results = [future.result(timeout=5) for future in futures]
    assert sorted(result.outcome for result in results) == ["committed", "conflict"]
    assert db.query("SELECT count(*) AS n FROM admission_operations") == [{"n": 1}]
    assert db.query("SELECT count(*) AS n FROM admission_requests") == [{"n": 1}]
    assert int(db.query("SELECT SUM(held_attempts) AS n FROM workspace_quotas")[0]["n"]) == 2
    assert (
        int(
            db.query(
                "SELECT SUM(held_slots) AS n FROM engine_instances "
                "WHERE instance_id IN ('instance-new','instance-discrete')"
            )[0]["n"]
        )
        == 1
    )
