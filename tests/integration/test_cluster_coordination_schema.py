"""Behavioral checks for the P0-B PostgreSQL coordination invariants."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import CheckViolation, ExclusionViolation, ForeignKeyViolation, UniqueViolation

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_DDL_PATH = _ROOT / "migrations" / "0001_cluster_coordination_draft.sql"
_DIGEST = "sha256:" + "a" * 64
_DATABASE_URL_ENV = "SPEECHNEXUS_TEST_DATABASE_URL"


@dataclass(frozen=True)
class CoordinationDatabase:
    url: str
    schema: str


def _database_url() -> str:
    value = os.environ.get(_DATABASE_URL_ENV)
    if not value:
        pytest.skip(f"set {_DATABASE_URL_ENV} to an isolated PostgreSQL test database")
    return value


@pytest.fixture(scope="module")
def coordination_schema() -> Iterator[CoordinationDatabase]:
    url = _database_url()
    schema = f"speechnexus_test_{uuid.uuid4().hex[:12]}"

    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public")
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        connection.execute(_DDL_PATH.read_text(encoding="utf-8"))

    try:
        yield CoordinationDatabase(url=url, schema=schema)
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def db(coordination_schema: CoordinationDatabase) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(coordination_schema.url) as connection:
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(
                sql.Identifier(coordination_schema.schema)
            )
        )
        yield connection
        connection.rollback()


def _insert_host(
    connection: psycopg.Connection[Any],
    host_node_id: str,
    *,
    architecture: str = "discrete_device_memory",
    node_state: str = "ready",
    allocatable_bytes: int = 1_000,
) -> None:
    unified = allocatable_bytes if architecture == "unified_memory" else 0
    device = allocatable_bytes if architecture == "discrete_device_memory" else 0
    host = allocatable_bytes if architecture == "discrete_device_memory" else 0
    connection.execute(
        """
        INSERT INTO host_nodes (
            host_node_id,
            host_node_profile_id,
            memory_architecture,
            node_state,
            topology_digest,
            allocatable_unified_memory_bytes,
            allocatable_device_memory_bytes,
            allocatable_host_memory_bytes,
            allocatable_cpu_decode_millicores,
            allocatable_host_pcm_buffer_bytes,
            allocatable_scratch_disk_bytes
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            host_node_id,
            "profile-test",
            architecture,
            node_state,
            _DIGEST,
            unified,
            device,
            host,
            allocatable_bytes,
            allocatable_bytes,
            allocatable_bytes,
        ),
    )


def _insert_pool(
    connection: psycopg.Connection[Any],
    *,
    pool_id: str = "asr-realtime",
    service_class: str = "realtime",
) -> None:
    connection.execute(
        "INSERT INTO resource_pools (pool_id, service_class) VALUES (%s, %s)",
        (pool_id, service_class),
    )


def _insert_instance(
    connection: psycopg.Connection[Any],
    *,
    instance_id: str,
    generation: int,
    host_node_id: str,
    architecture: str,
    pool_id: str = "asr-realtime",
    state: str = "ready",
    unified: tuple[int, int, int] = (0, 0, 0),
    device: tuple[int, int, int] = (100, 100, 10),
    host: tuple[int, int, int] = (100, 100, 10),
) -> None:
    connection.execute(
        """
        INSERT INTO engine_instances (
            instance_id,
            instance_generation,
            host_node_id,
            memory_architecture,
            gateway_id,
            pool_id,
            binding_revision_id,
            manifest_id,
            approved_template_id,
            config_digest,
            loading_policy,
            management_mode,
            instance_state,
            resident_unified_memory_bytes,
            loading_peak_unified_memory_bytes,
            per_slot_unified_memory_bytes,
            resident_device_memory_bytes,
            loading_peak_device_memory_bytes,
            per_slot_device_memory_bytes,
            resident_host_memory_bytes,
            loading_peak_host_memory_bytes,
            per_slot_host_memory_bytes,
            max_active_slots,
            last_fencing_token
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
        """,
        (
            instance_id,
            generation,
            host_node_id,
            architecture,
            "gateway-1",
            pool_id,
            "asr-quality@7",
            "manifest-1",
            "template-1",
            _DIGEST,
            "resident",
            "managed",
            state,
            unified[0],
            unified[1],
            unified[2],
            device[0],
            device[1],
            device[2],
            host[0],
            host[1],
            host[2],
            4,
            1,
        ),
    )


def _insert_lease(
    connection: psycopg.Connection[Any],
    *,
    lease_id: str,
    operation_id: str,
    attempt_id: str,
    stage_id: str = "stage-main",
    host_node_id: str,
    instance_id: str,
    generation: int,
    service_class: str = "realtime",
    pool_id: str = "asr-realtime",
    memory_architecture: str = "discrete_device_memory",
) -> None:
    reserved_unified = 1 if memory_architecture == "unified_memory" else 0
    reserved_device = 0 if memory_architecture == "unified_memory" else 1
    reserved_host = 0 if memory_architecture == "unified_memory" else 1
    connection.execute(
        """
        INSERT INTO admission_leases (
            lease_id,
            operation_id,
            attempt_id,
            stage_id,
            workspace_id,
            service_class,
            pool_id,
            host_node_id,
            memory_architecture,
            gateway_id,
            instance_id,
            instance_generation,
            binding_revision_id,
            policy_revision_id,
            fencing_token,
            lease_state,
            cleanup_state,
            reserved_unified_memory_bytes,
            reserved_device_memory_bytes,
            reserved_host_memory_bytes,
            reserved_cpu_decode_millicores,
            reserved_host_pcm_buffer_bytes,
            reserved_scratch_disk_bytes,
            deadline_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            CURRENT_TIMESTAMP + INTERVAL '1 hour'
        )
        """,
        (
            lease_id,
            operation_id,
            attempt_id,
            stage_id,
            "ws-default",
            service_class,
            pool_id,
            host_node_id,
            memory_architecture,
            "gateway-1",
            instance_id,
            generation,
            "asr-quality@7",
            "policy-1",
            1,
            "reserved",
            "pending",
            reserved_unified,
            reserved_device,
            reserved_host,
            1,
            1,
            1,
        ),
    )


def _insert_submit_intent(
    connection: psycopg.Connection[Any],
    *,
    submit_intent_id: str,
    lease_id: str,
    operation_id: str,
    attempt_id: str,
    host_node_id: str,
    instance_id: str,
    generation: int,
    stage_id: str = "stage-main",
    pool_id: str = "asr-realtime",
    workspace_id: str = "ws-default",
    gateway_id: str = "gateway-1",
    binding_revision_id: str = "asr-quality@7",
    generated_pcm_samples: int = 0,
) -> None:
    connection.execute(
        """
        INSERT INTO submit_intents (
            submit_intent_id,
            lease_id,
            operation_id,
            attempt_id,
            stage_id,
            workspace_id,
            pool_id,
            host_node_id,
            gateway_id,
            instance_id,
            instance_generation,
            binding_revision_id,
            policy_revision_id,
            fencing_token,
            command_digest,
            output_stream_id,
            sample_rate_hz,
            generated_pcm_samples,
            execution_state,
            cleanup_state,
            deadline_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            CURRENT_TIMESTAMP + INTERVAL '1 hour'
        )
        """,
        (
            submit_intent_id,
            lease_id,
            operation_id,
            attempt_id,
            stage_id,
            workspace_id,
            pool_id,
            host_node_id,
            gateway_id,
            instance_id,
            generation,
            binding_revision_id,
            "policy-1",
            1,
            _DIGEST,
            "tts-main",
            24_000,
            generated_pcm_samples,
            "executing",
            "pending",
        ),
    )


def _insert_interval(
    connection: psycopg.Connection[Any],
    *,
    interval_id: str,
    submit_intent_id: str,
    operation_id: str,
    attempt_id: str,
    sample_start: int,
    sample_end: int,
    meter_kind: str = "generated",
    output_stream_id: str = "tts-main",
    sample_rate_hz: int = 24_000,
) -> None:
    connection.execute(
        """
        INSERT INTO output_sample_intervals (
            interval_id,
            submit_intent_id,
            operation_id,
            attempt_id,
            output_stream_id,
            sample_rate_hz,
            sample_start,
            sample_end,
            meter_kind
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            interval_id,
            submit_intent_id,
            operation_id,
            attempt_id,
            output_stream_id,
            sample_rate_hz,
            sample_start,
            sample_end,
            meter_kind,
        ),
    )


def test_ddl_creates_coordination_and_interval_tables(
    db: psycopg.Connection[Any],
) -> None:
    rows = db.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = current_schema()
        ORDER BY table_name
        """
    ).fetchall()
    table_names = {row[0] for row in rows}
    assert {
        "admission_leases",
        "engine_instances",
        "gateway_fencing_barriers",
        "host_nodes",
        "output_sample_intervals",
        "resource_pools",
        "runtime_instance_intents",
        "scheduler_pool_leases",
        "submit_intents",
    } <= table_names


def test_instance_generation_history_is_retained(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-generation")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-generation",
        generation=1,
        host_node_id="node-generation",
        architecture="discrete_device_memory",
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET instance_state = 'stopped'
            WHERE instance_id = 'inst-generation' AND instance_generation = 1
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET instance_state = 'stopped', stopped_at = CURRENT_TIMESTAMP
            WHERE instance_id = 'inst-generation' AND instance_generation = 1
            """
        )
    db.execute(
        """
        UPDATE engine_instances
        SET instance_state = 'stopped',
            stopped_at = CURRENT_TIMESTAMP,
            peak_reservation_active = FALSE
        WHERE instance_id = 'inst-generation' AND instance_generation = 1
        """
    )
    _insert_instance(
        db,
        instance_id="inst-generation",
        generation=2,
        host_node_id="node-generation",
        architecture="discrete_device_memory",
    )

    with pytest.raises(UniqueViolation), db.transaction():
        _insert_instance(
            db,
            instance_id="inst-generation",
            generation=3,
            host_node_id="node-generation",
            architecture="discrete_device_memory",
        )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET instance_state = 'ready', stopped_at = NULL
            WHERE instance_id = 'inst-generation' AND instance_generation = 1
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET config_digest = %s
            WHERE instance_id = 'inst-generation' AND instance_generation = 2
            """,
            ("sha256:" + "b" * 64,),
        )


def test_unresolved_intents_are_unique_while_history_is_retained(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-intent")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-intent",
        generation=1,
        host_node_id="node-intent",
        architecture="discrete_device_memory",
    )
    insert_sql = """
        INSERT INTO runtime_instance_intents (
            intent_id,
            target_node_id,
            pool_id,
            gateway_id,
            instance_id,
            instance_generation,
            memory_architecture,
            approved_template_id,
            binding_revision_id,
            manifest_id,
            config_digest,
            action,
            fencing_token,
            intent_state,
            observed_instance_state,
            reserved_peak_device_bytes,
            reserved_steady_device_bytes,
            reserved_peak_host_bytes,
            reserved_steady_host_bytes,
            reserved_peak_unified_bytes,
            reserved_steady_unified_bytes,
            deadline_at
        ) VALUES (
            %s, 'node-intent', 'asr-realtime', 'gateway-1', 'inst-intent', 1,
            'discrete_device_memory', 'template-1', 'asr-quality@7',
            'manifest-1', %s, 'prepare', 1, %s, %s,
            20, 10, 20, 10, 0, 0, CURRENT_TIMESTAMP + INTERVAL '1 hour'
        )
    """
    db.execute(insert_sql, ("intent-history", _DIGEST, "succeeded", "ready"))
    db.execute(insert_sql, ("intent-active", _DIGEST, "pending", "loading"))

    with pytest.raises(UniqueViolation), db.transaction():
        db.execute(insert_sql, ("intent-duplicate", _DIGEST, "reconciling", "loading"))
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE runtime_instance_intents
            SET intent_state = 'pending'
            WHERE intent_id = 'intent-history'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE runtime_instance_intents
            SET config_digest = %s
            WHERE intent_id = 'intent-active'
            """,
            ("sha256:" + "b" * 64,),
        )


def test_memory_architecture_counters_are_exclusive(
    db: psycopg.Connection[Any],
) -> None:
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            INSERT INTO host_nodes (
                host_node_id, host_node_profile_id, memory_architecture,
                node_state, topology_digest, allocatable_unified_memory_bytes,
                allocatable_device_memory_bytes, allocatable_host_memory_bytes,
                allocatable_cpu_decode_millicores,
                allocatable_host_pcm_buffer_bytes, allocatable_scratch_disk_bytes
            ) VALUES (
                'node-invalid-unified', 'profile-test', 'unified_memory',
                'ready', %s, 1000, 1, 0, 1000, 1000, 1000
            )
            """,
            (_DIGEST,),
        )

    _insert_host(db, "node-unified", architecture="unified_memory")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-unified",
        generation=1,
        host_node_id="node-unified",
        architecture="unified_memory",
        unified=(100, 100, 10),
        device=(0, 0, 0),
        host=(0, 0, 0),
    )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_instance(
            db,
            instance_id="inst-unified-invalid",
            generation=1,
            host_node_id="node-unified",
            architecture="unified_memory",
            unified=(100, 100, 10),
            device=(1, 1, 1),
            host=(0, 0, 0),
        )

    _insert_host(db, "node-discrete")
    _insert_instance(
        db,
        instance_id="inst-discrete",
        generation=1,
        host_node_id="node-discrete",
        architecture="discrete_device_memory",
    )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_instance(
            db,
            instance_id="inst-discrete-invalid",
            generation=1,
            host_node_id="node-discrete",
            architecture="discrete_device_memory",
            unified=(1, 1, 1),
            device=(100, 100, 10),
            host=(100, 100, 10),
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_instance(
            db,
            instance_id="inst-mismatched-architecture",
            generation=1,
            host_node_id="node-unified",
            architecture="discrete_device_memory",
        )


def test_quarantine_is_counted_and_blocks_ready_state(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-quarantine", architecture="unified_memory")

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE host_nodes
            SET quarantined_unified_memory_bytes = 260
            WHERE host_node_id = 'node-quarantine'
            """
        )

    db.execute(
        """
        UPDATE host_nodes
        SET node_state = 'quarantined',
            quarantined_unified_memory_bytes = 260
        WHERE host_node_id = 'node-quarantine'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE host_nodes
            SET reserved_resident_unified_memory_bytes = 800
            WHERE host_node_id = 'node-quarantine'
            """
        )

    db.execute(
        """
        UPDATE host_nodes
        SET quarantined_unified_memory_bytes = 0,
            node_state = 'ready'
        WHERE host_node_id = 'node-quarantine'
        """
    )

    dimensions = (
        "quarantined_device_memory_bytes",
        "quarantined_host_memory_bytes",
        "quarantined_cpu_decode_millicores",
        "quarantined_host_pcm_buffer_bytes",
        "quarantined_scratch_disk_bytes",
    )
    for index, dimension in enumerate(dimensions):
        node_id = f"node-quarantine-{index}"
        _insert_host(db, node_id)
        update = sql.SQL("UPDATE host_nodes SET {} = 260 WHERE host_node_id = %s").format(
            sql.Identifier(dimension)
        )
        with pytest.raises(CheckViolation), db.transaction():
            db.execute(update, (node_id,))
        db.execute(
            sql.SQL(
                """
                UPDATE host_nodes
                SET node_state = 'quarantined', {} = 260
                WHERE host_node_id = %s
                """
            ).format(sql.Identifier(dimension)),
            (node_id,),
        )


def test_quarantined_lease_requires_evidence_to_release(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-release")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-release",
        generation=1,
        host_node_id="node-release",
        architecture="discrete_device_memory",
    )
    _insert_lease(
        db,
        lease_id="lease-release",
        operation_id="op-release",
        attempt_id="att-release",
        host_node_id="node-release",
        instance_id="inst-release",
        generation=1,
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET quarantine_release_evidence_kind = 'operator_verified_instance_terminated',
                quarantine_release_evidence_digest = %s
            WHERE lease_id = 'lease-release'
            """,
            (_DIGEST,),
        )
    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'quarantined',
            cleanup_state = 'quarantined',
            quarantined_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-release'
        """
    )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET lease_state = 'released',
                cleanup_state = 'complete',
                budget_released_at = CURRENT_TIMESTAMP
            WHERE lease_id = 'lease-release'
            """
        )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET quarantine_release_evidence_kind = 'operator_verified_instance_terminated',
                quarantine_release_evidence_digest = %s,
                quarantine_release_observed_at = CURRENT_TIMESTAMP
            WHERE lease_id = 'lease-release'
            """,
            (_DIGEST,),
        )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET quarantine_release_evidence_kind = 'operator_verified_instance_terminated',
                quarantine_release_evidence_digest = %s,
                quarantine_release_observed_at = CURRENT_TIMESTAMP,
                quarantine_release_reason = '   '
            WHERE lease_id = 'lease-release'
            """,
            (_DIGEST,),
        )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET lease_state = 'released',
                cleanup_state = 'complete',
                quarantine_release_evidence_kind = 'operator_verified_instance_terminated',
                quarantine_release_evidence_digest = %s,
                quarantine_release_observed_at = CURRENT_TIMESTAMP + INTERVAL '1 hour',
                quarantine_release_reason = 'operator confirmed the old container exited',
                budget_released_at = CURRENT_TIMESTAMP
            WHERE lease_id = 'lease-release'
            """,
            (_DIGEST,),
        )

    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'released',
            cleanup_state = 'complete',
            quarantine_release_evidence_kind = 'operator_verified_instance_terminated',
            quarantine_release_evidence_digest = %s,
            quarantine_release_observed_at = CURRENT_TIMESTAMP - INTERVAL '1 second',
            quarantine_release_reason = 'operator confirmed the old container exited',
            budget_released_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-release'
        """,
        (_DIGEST,),
    )
    row = db.execute(
        """
        SELECT lease_state,
               cleanup_state,
               budget_released_at,
               quarantine_release_observed_at,
               quarantine_release_reason
        FROM admission_leases
        WHERE lease_id = 'lease-release'
        """
    ).fetchone()
    assert row is not None
    assert row[0:2] == ("released", "complete")
    assert isinstance(row[2], datetime)
    assert isinstance(row[3], datetime)
    assert row[2] >= row[3]
    assert row[4] == "operator confirmed the old container exited"

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET lease_state = 'reserved',
                cleanup_state = 'pending',
                budget_released_at = NULL
            WHERE lease_id = 'lease-release'
            """
        )

    _insert_lease(
        db,
        lease_id="lease-reservation-immutable",
        operation_id="op-reservation-immutable",
        attempt_id="att-reservation-immutable",
        host_node_id="node-release",
        instance_id="inst-release",
        generation=1,
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET reserved_slots = 2
            WHERE lease_id = 'lease-reservation-immutable'
            """
        )


def test_service_class_and_submit_identity_cannot_drift(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-identity")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-identity",
        generation=1,
        host_node_id="node-identity",
        architecture="discrete_device_memory",
    )

    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_lease(
            db,
            lease_id="lease-wrong-class",
            operation_id="op-wrong-class",
            attempt_id="att-wrong-class",
            host_node_id="node-identity",
            instance_id="inst-identity",
            generation=1,
            service_class="batch",
        )
    _insert_pool(db, pool_id="tts-batch", service_class="batch")
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_lease(
            db,
            lease_id="lease-wrong-pool",
            operation_id="op-wrong-pool",
            attempt_id="att-wrong-pool",
            host_node_id="node-identity",
            instance_id="inst-identity",
            generation=1,
            service_class="batch",
            pool_id="tts-batch",
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_lease(
            db,
            lease_id="lease-wrong-architecture",
            operation_id="op-wrong-architecture",
            attempt_id="att-wrong-architecture",
            host_node_id="node-identity",
            instance_id="inst-identity",
            generation=1,
            memory_architecture="unified_memory",
        )

    _insert_lease(
        db,
        lease_id="lease-identity",
        operation_id="op-identity",
        attempt_id="att-identity",
        host_node_id="node-identity",
        instance_id="inst-identity",
        generation=1,
    )
    _insert_host(db, "node-identity-other")
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-wrong-operation",
            lease_id="lease-identity",
            operation_id="op-other",
            attempt_id="att-identity",
            host_node_id="node-identity",
            instance_id="inst-identity",
            generation=1,
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-wrong-pool",
            lease_id="lease-identity",
            operation_id="op-identity",
            attempt_id="att-identity",
            host_node_id="node-identity",
            instance_id="inst-identity",
            generation=1,
            pool_id="tts-batch",
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-wrong-workspace",
            lease_id="lease-identity",
            operation_id="op-identity",
            attempt_id="att-identity",
            host_node_id="node-identity",
            instance_id="inst-identity",
            generation=1,
            workspace_id="ws-other",
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-wrong-host",
            lease_id="lease-identity",
            operation_id="op-identity",
            attempt_id="att-identity",
            host_node_id="node-identity-other",
            instance_id="inst-identity",
            generation=1,
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-wrong-gateway",
            lease_id="lease-identity",
            operation_id="op-identity",
            attempt_id="att-identity",
            host_node_id="node-identity",
            instance_id="inst-identity",
            generation=1,
            gateway_id="gateway-other",
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-wrong-binding",
            lease_id="lease-identity",
            operation_id="op-identity",
            attempt_id="att-identity",
            host_node_id="node-identity",
            instance_id="inst-identity",
            generation=1,
            binding_revision_id="asr-quality@8",
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-wrong-stage",
            lease_id="lease-identity",
            operation_id="op-identity",
            attempt_id="att-identity",
            host_node_id="node-identity",
            instance_id="inst-identity",
            generation=1,
            stage_id="stage-other",
        )


def test_one_attempt_can_reserve_multiple_stages_in_the_same_pool(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-multi-stage")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-multi-stage",
        generation=1,
        host_node_id="node-multi-stage",
        architecture="discrete_device_memory",
    )
    for stage_id, lease_id in (
        ("stage-vad", "lease-stage-vad"),
        ("stage-asr-preview", "lease-stage-asr-preview"),
    ):
        _insert_lease(
            db,
            lease_id=lease_id,
            operation_id="op-multi-stage",
            attempt_id="att-multi-stage",
            stage_id=stage_id,
            host_node_id="node-multi-stage",
            instance_id="inst-multi-stage",
            generation=1,
        )
        _insert_submit_intent(
            db,
            submit_intent_id=f"intent-{stage_id}",
            lease_id=lease_id,
            operation_id="op-multi-stage",
            attempt_id="att-multi-stage",
            stage_id=stage_id,
            host_node_id="node-multi-stage",
            instance_id="inst-multi-stage",
            generation=1,
        )

    assert db.execute(
        """
        SELECT count(*)
        FROM admission_leases
        WHERE operation_id = 'op-multi-stage' AND attempt_id = 'att-multi-stage'
        """
    ).fetchone() == (2,)
    assert db.execute(
        """
        SELECT count(*)
        FROM submit_intents
        WHERE operation_id = 'op-multi-stage' AND attempt_id = 'att-multi-stage'
        """
    ).fetchone() == (2,)

    with pytest.raises(UniqueViolation), db.transaction():
        _insert_lease(
            db,
            lease_id="lease-stage-vad-duplicate",
            operation_id="op-multi-stage",
            attempt_id="att-multi-stage",
            stage_id="stage-vad",
            host_node_id="node-multi-stage",
            instance_id="inst-multi-stage",
            generation=1,
        )


@pytest.mark.parametrize(
    "evidence_kind",
    ["engine_terminal_response", "operator_verified_instance_terminated"],
)
def test_quarantined_execution_closes_with_terminal_or_operator_evidence(
    db: psycopg.Connection[Any],
    evidence_kind: str,
) -> None:
    _insert_host(db, "node-stop-evidence")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-stop-evidence",
        generation=1,
        host_node_id="node-stop-evidence",
        architecture="discrete_device_memory",
    )
    _insert_lease(
        db,
        lease_id="lease-stop-evidence",
        operation_id="op-stop-evidence",
        attempt_id="att-stop-evidence",
        host_node_id="node-stop-evidence",
        instance_id="inst-stop-evidence",
        generation=1,
    )
    _insert_submit_intent(
        db,
        submit_intent_id="intent-stop-evidence",
        lease_id="lease-stop-evidence",
        operation_id="op-stop-evidence",
        attempt_id="att-stop-evidence",
        host_node_id="node-stop-evidence",
        instance_id="inst-stop-evidence",
        generation=1,
    )
    db.execute(
        """
        UPDATE submit_intents
        SET execution_state = 'unknown_quarantined',
            cleanup_state = 'quarantined',
            stop_evidence_kind = 'unconfirmed_timeout'
        WHERE submit_intent_id = 'intent-stop-evidence'
        """
    )
    db.execute(
        """
        UPDATE submit_intents
        SET execution_state = 'aborted',
            cleanup_state = 'complete',
            stop_evidence_kind = %s,
            terminated_at = CURRENT_TIMESTAMP
        WHERE submit_intent_id = 'intent-stop-evidence'
        """,
        (evidence_kind,),
    )
    row = db.execute(
        """
        SELECT execution_state, cleanup_state, stop_evidence_kind
        FROM submit_intents
        WHERE submit_intent_id = 'intent-stop-evidence'
        """
    ).fetchone()
    assert row == ("aborted", "complete", evidence_kind)

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET execution_state = 'executing',
                cleanup_state = 'pending',
                terminated_at = NULL
            WHERE submit_intent_id = 'intent-stop-evidence'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET command_digest = %s
            WHERE submit_intent_id = 'intent-stop-evidence'
            """,
            ("sha256:" + "b" * 64,),
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET stop_evidence_kind = 'engine_abort_ack'
            WHERE submit_intent_id = 'intent-stop-evidence'
            """
        )


def test_submit_execution_cleanup_matrix_is_closed(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-cleanup-matrix")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-cleanup-matrix",
        generation=1,
        host_node_id="node-cleanup-matrix",
        architecture="discrete_device_memory",
    )
    for suffix in ("success", "failure"):
        _insert_lease(
            db,
            lease_id=f"lease-cleanup-{suffix}",
            operation_id=f"op-cleanup-{suffix}",
            attempt_id=f"att-cleanup-{suffix}",
            host_node_id="node-cleanup-matrix",
            instance_id="inst-cleanup-matrix",
            generation=1,
        )
        _insert_submit_intent(
            db,
            submit_intent_id=f"intent-cleanup-{suffix}",
            lease_id=f"lease-cleanup-{suffix}",
            operation_id=f"op-cleanup-{suffix}",
            attempt_id=f"att-cleanup-{suffix}",
            host_node_id="node-cleanup-matrix",
            instance_id="inst-cleanup-matrix",
            generation=1,
        )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET cleanup_state = 'complete'
            WHERE submit_intent_id = 'intent-cleanup-success'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET execution_state = 'succeeded',
                cleanup_state = 'complete',
                stop_evidence_kind = 'none',
                terminated_at = CURRENT_TIMESTAMP
            WHERE submit_intent_id = 'intent-cleanup-success'
            """
        )
    db.execute(
        """
        UPDATE submit_intents
        SET execution_state = 'succeeded',
            cleanup_state = 'complete',
            stop_evidence_kind = 'engine_terminal_response',
            terminated_at = CURRENT_TIMESTAMP
        WHERE submit_intent_id = 'intent-cleanup-success'
        """
    )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET execution_state = 'failed',
                cleanup_state = 'complete',
                stop_evidence_kind = 'none',
                error_code = 'policy_or_resource_disabled',
                terminated_at = CURRENT_TIMESTAMP
            WHERE submit_intent_id = 'intent-cleanup-failure'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET execution_state = 'failed',
                cleanup_state = 'not_required',
                stop_evidence_kind = 'none'
            WHERE submit_intent_id = 'intent-cleanup-failure'
            """
        )
    db.execute(
        """
        UPDATE submit_intents
        SET execution_state = 'failed',
            cleanup_state = 'complete',
            stop_evidence_kind = 'instance_terminated_by_controller',
            error_code = 'policy_or_resource_disabled',
            terminated_at = CURRENT_TIMESTAMP
        WHERE submit_intent_id = 'intent-cleanup-failure'
        """
    )


def test_admission_reservations_and_fencing_cannot_be_negative(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-negative")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-negative",
        generation=1,
        host_node_id="node-negative",
        architecture="discrete_device_memory",
    )
    _insert_lease(
        db,
        lease_id="lease-negative",
        operation_id="op-negative",
        attempt_id="att-negative",
        host_node_id="node-negative",
        instance_id="inst-negative",
        generation=1,
    )

    for assignment in (
        "reserved_slots = 0",
        "reserved_device_memory_bytes = -1",
        "fencing_token = 0",
    ):
        with pytest.raises(CheckViolation), db.transaction():
            db.execute(
                f"UPDATE admission_leases SET {assignment} WHERE lease_id = 'lease-negative'"
            )


def test_fencing_is_monotonic_and_updated_at_is_refreshed(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-fencing")
    _insert_pool(db)
    db.execute(
        """
        UPDATE resource_pools
        SET high_water_fencing_token = 7
        WHERE pool_id = 'asr-realtime'
        """
    )
    db.execute(
        """
        INSERT INTO scheduler_pool_leases (
            pool_id,
            owner_scheduler_id,
            fencing_token,
            lease_state,
            acquired_at,
            renewed_at,
            expires_at,
            previous_owner_reconciled
        ) VALUES (
            'asr-realtime',
            'scheduler-1',
            7,
            'active',
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP + INTERVAL '1 hour',
            TRUE
        )
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE scheduler_pool_leases
            SET fencing_token = 6
            WHERE pool_id = 'asr-realtime'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE scheduler_pool_leases
            SET owner_scheduler_id = 'scheduler-2', fencing_token = 7
            WHERE pool_id = 'asr-realtime'
            """
        )
    db.execute(
        """
        UPDATE scheduler_pool_leases
        SET owner_scheduler_id = 'scheduler-2', fencing_token = 8
        WHERE pool_id = 'asr-realtime'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE resource_pools
            SET high_water_fencing_token = 6
            WHERE pool_id = 'asr-realtime'
            """
        )
    db.execute(
        """
        INSERT INTO gateway_fencing_barriers (
            gateway_id,
            pool_id,
            host_node_id,
            enforced_min_fencing_token,
            new_owner_scheduler_id
        ) VALUES ('gateway-1', 'asr-realtime', 'node-fencing', 5, 'scheduler-2')
        """
    )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE gateway_fencing_barriers
            SET enforced_min_fencing_token = 4
            WHERE gateway_id = 'gateway-1' AND pool_id = 'asr-realtime'
            """
        )
    db.execute(
        """
        INSERT INTO engine_instances (
            instance_id, instance_generation, host_node_id, memory_architecture,
            gateway_id, pool_id, binding_revision_id, manifest_id,
            approved_template_id, config_digest, loading_policy, management_mode,
            instance_state, resident_unified_memory_bytes,
            loading_peak_unified_memory_bytes, per_slot_unified_memory_bytes,
            resident_device_memory_bytes, loading_peak_device_memory_bytes,
            per_slot_device_memory_bytes, resident_host_memory_bytes,
            loading_peak_host_memory_bytes, per_slot_host_memory_bytes,
            max_active_slots, last_fencing_token
        ) VALUES (
            'inst-fencing', 1, 'node-fencing', 'discrete_device_memory',
            'gateway-1', 'asr-realtime', 'asr-quality@7', 'manifest-1',
            'template-1', %s, 'resident', 'managed', 'ready',
            0, 0, 0, 100, 100, 10, 100, 100, 10, 4, 7
        )
        """,
        (_DIGEST,),
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET last_fencing_token = 6
            WHERE instance_id = 'inst-fencing' AND instance_generation = 1
            """
        )
    db.execute(
        """
        UPDATE gateway_fencing_barriers
        SET enforced_min_fencing_token = 6
        WHERE gateway_id = 'gateway-1' AND pool_id = 'asr-realtime'
        """
    )
    db.execute(
        """
        UPDATE host_nodes
        SET node_state = 'draining', updated_at = TIMESTAMPTZ '2000-01-01'
        WHERE host_node_id = 'node-fencing'
        """
    )
    row = db.execute(
        "SELECT updated_at FROM host_nodes WHERE host_node_id = 'node-fencing'"
    ).fetchone()
    assert row is not None
    assert isinstance(row[0], datetime)
    assert row[0].year > 2000

    trigger_rows = db.execute(
        """
        SELECT event_object_table
        FROM information_schema.triggers
        WHERE trigger_schema = current_schema()
          AND trigger_name LIKE 'trg_%_updated_at'
        ORDER BY event_object_table
        """
    ).fetchall()
    assert {row[0] for row in trigger_rows} == {
        "admission_leases",
        "engine_instances",
        "gateway_fencing_barriers",
        "host_nodes",
        "resource_pools",
        "runtime_instance_intents",
        "submit_intents",
    }


def test_output_intervals_are_non_overlapping_and_identity_bound(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-metering")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-metering",
        generation=1,
        host_node_id="node-metering",
        architecture="discrete_device_memory",
    )
    _insert_lease(
        db,
        lease_id="lease-metering",
        operation_id="op-metering",
        attempt_id="att-metering",
        host_node_id="node-metering",
        instance_id="inst-metering",
        generation=1,
    )
    _insert_submit_intent(
        db,
        submit_intent_id="intent-metering",
        lease_id="lease-metering",
        operation_id="op-metering",
        attempt_id="att-metering",
        host_node_id="node-metering",
        instance_id="inst-metering",
        generation=1,
    )
    db.execute(
        """
        UPDATE submit_intents
        SET hard_output_ceiling_samples = 48000
        WHERE submit_intent_id = 'intent-metering'
        """
    )
    _insert_interval(
        db,
        interval_id="interval-initial",
        submit_intent_id="intent-metering",
        operation_id="op-metering",
        attempt_id="att-metering",
        sample_start=0,
        sample_end=100,
    )
    watermark = db.execute(
        """
        SELECT generated_pcm_samples
        FROM submit_intents
        WHERE submit_intent_id = 'intent-metering'
        """
    ).fetchone()
    assert watermark == (100,)
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET generated_pcm_samples = 24000
            WHERE submit_intent_id = 'intent-metering'
            """
        )
    assert db.execute(
        """
            SELECT generated_pcm_samples
            FROM submit_intents
            WHERE submit_intent_id = 'intent-metering'
            """
    ).fetchone() == (100,)
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET generated_pcm_samples = 23999
            WHERE submit_intent_id = 'intent-metering'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET sample_rate_hz = 16000
            WHERE submit_intent_id = 'intent-metering'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET hard_output_ceiling_samples = 96000
            WHERE submit_intent_id = 'intent-metering'
            """
        )
    with pytest.raises(ExclusionViolation), db.transaction():
        _insert_interval(
            db,
            interval_id="interval-overlap",
            submit_intent_id="intent-metering",
            operation_id="op-metering",
            attempt_id="att-metering",
            sample_start=50,
            sample_end=150,
        )

    _insert_interval(
        db,
        interval_id="interval-adjacent",
        submit_intent_id="intent-metering",
        operation_id="op-metering",
        attempt_id="att-metering",
        sample_start=100,
        sample_end=200,
    )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_interval(
            db,
            interval_id="interval-identity-mismatch",
            submit_intent_id="intent-metering",
            operation_id="op-other",
            attempt_id="att-metering",
            sample_start=200,
            sample_end=300,
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_interval(
            db,
            interval_id="interval-output-mismatch",
            submit_intent_id="intent-metering",
            operation_id="op-metering",
            attempt_id="att-metering",
            sample_start=200,
            sample_end=300,
            output_stream_id="other-stream",
        )
    with pytest.raises(ForeignKeyViolation), db.transaction():
        _insert_interval(
            db,
            interval_id="interval-rate-mismatch",
            submit_intent_id="intent-metering",
            operation_id="op-metering",
            attempt_id="att-metering",
            sample_start=200,
            sample_end=300,
            sample_rate_hz=16_000,
        )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_interval(
            db,
            interval_id="interval-over-ceiling",
            submit_intent_id="intent-metering",
            operation_id="op-metering",
            attempt_id="att-metering",
            sample_start=47999,
            sample_end=48001,
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE output_sample_intervals
            SET sample_end = 101
            WHERE interval_id = 'interval-initial'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute("DELETE FROM output_sample_intervals WHERE interval_id = 'interval-initial'")
    with pytest.raises(CheckViolation), db.transaction():
        db.execute("TRUNCATE output_sample_intervals")

    _insert_lease(
        db,
        lease_id="lease-metering-insert",
        operation_id="op-metering-insert",
        attempt_id="att-metering-insert",
        host_node_id="node-metering",
        instance_id="inst-metering",
        generation=1,
    )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-metering-insert",
            lease_id="lease-metering-insert",
            operation_id="op-metering-insert",
            attempt_id="att-metering-insert",
            host_node_id="node-metering",
            instance_id="inst-metering",
            generation=1,
            generated_pcm_samples=100,
        )


def test_output_ceiling_cannot_be_introduced_after_generated_output(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-late-ceiling")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-late-ceiling",
        generation=1,
        host_node_id="node-late-ceiling",
        architecture="discrete_device_memory",
    )
    _insert_lease(
        db,
        lease_id="lease-late-ceiling",
        operation_id="op-late-ceiling",
        attempt_id="att-late-ceiling",
        host_node_id="node-late-ceiling",
        instance_id="inst-late-ceiling",
        generation=1,
    )
    _insert_submit_intent(
        db,
        submit_intent_id="intent-late-ceiling",
        lease_id="lease-late-ceiling",
        operation_id="op-late-ceiling",
        attempt_id="att-late-ceiling",
        host_node_id="node-late-ceiling",
        instance_id="inst-late-ceiling",
        generation=1,
    )
    _insert_interval(
        db,
        interval_id="interval-before-ceiling",
        submit_intent_id="intent-late-ceiling",
        operation_id="op-late-ceiling",
        attempt_id="att-late-ceiling",
        sample_start=0,
        sample_end=100,
    )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET hard_output_ceiling_samples = 48000
            WHERE submit_intent_id = 'intent-late-ceiling'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET sample_rate_hz = NULL,
                hard_output_ceiling_samples = 48000
            WHERE submit_intent_id = 'intent-late-ceiling'
            """
        )
