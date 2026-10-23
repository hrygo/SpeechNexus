"""Historical database-enforced rules; not the ADR 0006 transaction prototype."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import CheckViolation, ExclusionViolation, ForeignKeyViolation, UniqueViolation

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_DDL_PATH = _ROOT / "tests" / "fixtures" / "coordination" / "legacy_postgresql.sql"
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
    device: tuple[int, int, int] = (100, 100, 1),
    host: tuple[int, int, int] = (100, 100, 1),
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
            per_slot_cpu_decode_millicores,
            per_slot_host_pcm_buffer_bytes,
            per_slot_scratch_disk_bytes,
            max_active_slots,
            last_fencing_token,
            ready_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, 1, 1, 1, %s, %s,
            CASE WHEN %s = 'ready' THEN CURRENT_TIMESTAMP ELSE NULL END
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
            state,
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
    reservation: tuple[int, ...] | None = None,
    slots: int = 1,
) -> None:
    reserved_unified = 1 if memory_architecture == "unified_memory" else 0
    reserved_device = 0 if memory_architecture == "unified_memory" else 1
    reserved_host = 0 if memory_architecture == "unified_memory" else 1
    vector = reservation or (reserved_unified, reserved_device, reserved_host, 1, 1, 1)
    assert len(vector) == 6
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
            reserved_slots,
            deadline_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, CURRENT_TIMESTAMP + INTERVAL '1 hour'
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
            *vector,
            slots,
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
    dispatched: bool = True,
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
            engine_request_id,
            execution_state,
            cleanup_state,
            dispatched_at,
            deadline_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
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
            f"engine-request-{submit_intent_id}" if dispatched else None,
            "executing" if dispatched else "submitting",
            "pending",
            datetime.now(UTC) if dispatched else None,
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
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET resident_device_memory_bytes = 0
            WHERE instance_id = 'inst-generation'
              AND instance_generation = 2
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET max_active_slots = 8
            WHERE instance_id = 'inst-generation'
              AND instance_generation = 2
            """
        )


def test_instance_readiness_and_quarantine_reason_are_durable(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-instance-facts")
    _insert_pool(db)

    _insert_instance(
        db,
        instance_id="inst-ready-without-evidence",
        generation=1,
        host_node_id="node-instance-facts",
        architecture="discrete_device_memory",
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET ready_at = NULL
            WHERE instance_id = 'inst-ready-without-evidence'
              AND instance_generation = 1
            """
        )

    _insert_instance(
        db,
        instance_id="inst-quarantined-without-reason",
        generation=1,
        host_node_id="node-instance-facts",
        architecture="discrete_device_memory",
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET instance_state = 'quarantined',
                peak_reservation_active = FALSE
            WHERE instance_id = 'inst-quarantined-without-reason'
              AND instance_generation = 1
            """
        )
    db.execute(
        """
        UPDATE engine_instances
        SET instance_state = 'quarantined',
            peak_reservation_active = FALSE,
            quarantine_reason = 'synthetic unknown execution'
        WHERE instance_id = 'inst-quarantined-without-reason'
          AND instance_generation = 1
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET instance_state = 'stopping'
            WHERE instance_id = 'inst-quarantined-without-reason'
              AND instance_generation = 1
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET instance_state = 'stopping',
                quarantine_reason = NULL
            WHERE instance_id = 'inst-quarantined-without-reason'
              AND instance_generation = 1
            """
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

    # The idempotent request projection is frozen independently of observed state.
    for column in (
        "reserved_peak_device_bytes",
        "reserved_steady_device_bytes",
        "reserved_peak_host_bytes",
        "reserved_steady_host_bytes",
        "reserved_peak_unified_bytes",
        "reserved_steady_unified_bytes",
        "deadline_at",
    ):
        delta = "INTERVAL '1 second'" if column == "deadline_at" else "1"
        with pytest.raises(CheckViolation, match="runtime intent identity"), db.transaction():
            db.execute(
                sql.SQL(
                    "UPDATE runtime_instance_intents SET {c} = {c} + "
                    + delta
                    + " WHERE intent_id = 'intent-active'"
                ).format(c=sql.Identifier(column))
            )
    with pytest.raises(CheckViolation, match="append-only"), db.transaction():
        db.execute("DELETE FROM runtime_instance_intents WHERE intent_id = 'intent-active'")

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
            WHERE target_node_id = 'node-intent' AND intent_id = 'intent-active'
            """,
            ("sha256:" + "b" * 64,),
        )

    _insert_host(db, "node-intent-other")
    _insert_instance(
        db,
        instance_id="inst-intent-other",
        generation=1,
        host_node_id="node-intent-other",
        architecture="discrete_device_memory",
    )
    db.execute(
        """
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
            'intent-active', 'node-intent-other', 'asr-realtime', 'gateway-1',
            'inst-intent-other', 1, 'discrete_device_memory', 'template-1',
            'asr-quality@7', 'manifest-1', %s, 'prepare', 1, 'pending',
            'loading', 20, 10, 20, 10, 0, 0,
            CURRENT_TIMESTAMP + INTERVAL '1 hour'
        )
        """,
        (_DIGEST,),
    )
    assert db.execute(
        """
            SELECT COUNT(*)
            FROM runtime_instance_intents
            WHERE intent_id = 'intent-active'
            """
    ).fetchone() == (2,)

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE runtime_instance_intents
            SET intent_state = 'succeeded',
                observed_instance_state = 'ready',
                error_code = 'runtime_start_failed'
            WHERE target_node_id = 'node-intent' AND intent_id = 'intent-active'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE runtime_instance_intents
            SET intent_state = 'failed', observed_instance_state = 'failed'
            WHERE target_node_id = 'node-intent' AND intent_id = 'intent-active'
            """
        )
    db.execute(
        """
        UPDATE runtime_instance_intents
        SET intent_state = 'failed',
            observed_instance_state = 'failed',
            error_code = 'runtime_start_failed'
        WHERE target_node_id = 'node-intent' AND intent_id = 'intent-active'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE runtime_instance_intents
            SET error_code = 'runtime_start_failed_again'
            WHERE target_node_id = 'node-intent' AND intent_id = 'intent-active'
            """
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
        unified=(100, 100, 1),
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
            unified=(100, 100, 1),
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


def test_capacity_aggregates_must_match_authoritative_rows(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-capacity-accounting")
    _insert_pool(db, pool_id="capacity-accounting")
    _insert_instance(
        db,
        instance_id="inst-capacity-accounting",
        generation=1,
        host_node_id="node-capacity-accounting",
        architecture="discrete_device_memory",
        pool_id="capacity-accounting",
    )
    _insert_lease(
        db,
        lease_id="lease-capacity-accounting",
        operation_id="op-capacity-accounting",
        attempt_id="att-capacity-accounting",
        host_node_id="node-capacity-accounting",
        instance_id="inst-capacity-accounting",
        generation=1,
        pool_id="capacity-accounting",
    )

    db.execute(
        """
        UPDATE host_nodes
        SET reserved_device_memory_bytes = 101,
            reserved_host_memory_bytes = 101,
            reserved_cpu_decode_millicores = 1,
            reserved_host_pcm_buffer_bytes = 1,
            reserved_scratch_disk_bytes = 1
        WHERE host_node_id = 'node-capacity-accounting'
        """
    )
    db.execute(
        """
        UPDATE engine_instances
        SET reserved_active_slots = 1
        WHERE instance_id = 'inst-capacity-accounting'
          AND instance_generation = 1
        """
    )
    db.commit()

    db.execute(
        """
        UPDATE host_nodes
        SET reserved_device_memory_bytes = 100
        WHERE host_node_id = 'node-capacity-accounting'
        """
    )
    with pytest.raises(CheckViolation):
        db.commit()
    db.rollback()

    db.execute(
        """
        UPDATE engine_instances
        SET reserved_active_slots = 0
        WHERE instance_id = 'inst-capacity-accounting'
          AND instance_generation = 1
        """
    )
    with pytest.raises(CheckViolation):
        db.commit()
    db.rollback()

    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'quarantined',
            cleanup_state = 'quarantined',
            quarantined_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-capacity-accounting'
        """
    )
    db.execute(
        """
        UPDATE engine_instances
        SET reserved_active_slots = 0,
            quarantined_slots = 1
        WHERE instance_id = 'inst-capacity-accounting'
          AND instance_generation = 1
        """
    )
    db.execute(
        """
        UPDATE host_nodes
        SET reserved_device_memory_bytes = 100,
            reserved_host_memory_bytes = 100,
            reserved_cpu_decode_millicores = 0,
            reserved_host_pcm_buffer_bytes = 0,
            reserved_scratch_disk_bytes = 0,
            quarantined_device_memory_bytes = 1,
            quarantined_host_memory_bytes = 1,
            quarantined_cpu_decode_millicores = 1,
            quarantined_host_pcm_buffer_bytes = 1,
            quarantined_scratch_disk_bytes = 1
        WHERE host_node_id = 'node-capacity-accounting'
        """
    )
    db.commit()

    db.execute(
        """
        UPDATE engine_instances
        SET quarantined_slots = 0
        WHERE instance_id = 'inst-capacity-accounting'
          AND instance_generation = 1
        """
    )
    with pytest.raises(CheckViolation):
        db.commit()
    db.rollback()

    with pytest.raises(CheckViolation), db.transaction():
        db.execute("DELETE FROM admission_leases WHERE lease_id = 'lease-capacity-accounting'")


def test_admission_requires_ready_topology(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-admission-topology")
    _insert_pool(db, pool_id="admission-topology")
    _insert_instance(
        db,
        instance_id="inst-admission-topology",
        generation=1,
        host_node_id="node-admission-topology",
        architecture="discrete_device_memory",
        pool_id="admission-topology",
        state="warming_up",
    )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_lease(
            db,
            lease_id="lease-admission-warming-up",
            operation_id="op-admission-warming-up",
            attempt_id="att-admission-warming-up",
            host_node_id="node-admission-topology",
            instance_id="inst-admission-topology",
            generation=1,
            pool_id="admission-topology",
        )

    db.execute(
        """
        UPDATE engine_instances
        SET instance_state = 'ready',
            ready_at = CURRENT_TIMESTAMP
        WHERE instance_id = 'inst-admission-topology'
          AND instance_generation = 1
        """
    )
    _insert_lease(
        db,
        lease_id="lease-admission-ready",
        operation_id="op-admission-ready",
        attempt_id="att-admission-ready",
        host_node_id="node-admission-topology",
        instance_id="inst-admission-topology",
        generation=1,
        pool_id="admission-topology",
    )

    db.execute(
        """
        UPDATE resource_pools
        SET pool_state = 'draining'
        WHERE pool_id = 'admission-topology'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_lease(
            db,
            lease_id="lease-admission-draining-pool",
            operation_id="op-admission-draining-pool",
            attempt_id="att-admission-draining-pool",
            host_node_id="node-admission-topology",
            instance_id="inst-admission-topology",
            generation=1,
            pool_id="admission-topology",
        )

    db.execute(
        """
        UPDATE resource_pools
        SET pool_state = 'active'
        WHERE pool_id = 'admission-topology'
        """
    )
    db.execute(
        """
        UPDATE host_nodes
        SET node_state = 'draining'
        WHERE host_node_id = 'node-admission-topology'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_lease(
            db,
            lease_id="lease-admission-draining-node",
            operation_id="op-admission-draining-node",
            attempt_id="att-admission-draining-node",
            host_node_id="node-admission-topology",
            instance_id="inst-admission-topology",
            generation=1,
            pool_id="admission-topology",
        )

    db.execute(
        """
        UPDATE host_nodes
        SET node_state = 'ready'
        WHERE host_node_id = 'node-admission-topology'
        """
    )
    db.execute(
        """
        UPDATE engine_instances
        SET instance_state = 'draining'
        WHERE instance_id = 'inst-admission-topology'
          AND instance_generation = 1
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_lease(
            db,
            lease_id="lease-admission-draining-instance",
            operation_id="op-admission-draining-instance",
            attempt_id="att-admission-draining-instance",
            host_node_id="node-admission-topology",
            instance_id="inst-admission-topology",
            generation=1,
            pool_id="admission-topology",
        )


def test_unified_capacity_aggregates_move_with_quarantine(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(
        db,
        "node-unified-capacity-accounting",
        architecture="unified_memory",
    )
    _insert_pool(db, pool_id="unified-capacity-accounting")
    _insert_instance(
        db,
        instance_id="inst-unified-capacity-accounting",
        generation=1,
        host_node_id="node-unified-capacity-accounting",
        architecture="unified_memory",
        pool_id="unified-capacity-accounting",
        unified=(100, 100, 1),
        device=(0, 0, 0),
        host=(0, 0, 0),
    )
    _insert_lease(
        db,
        lease_id="lease-unified-capacity-accounting",
        operation_id="op-unified-capacity-accounting",
        attempt_id="att-unified-capacity-accounting",
        host_node_id="node-unified-capacity-accounting",
        instance_id="inst-unified-capacity-accounting",
        generation=1,
        service_class="realtime",
        pool_id="unified-capacity-accounting",
        memory_architecture="unified_memory",
    )

    db.execute(
        """
        UPDATE engine_instances
        SET peak_reservation_active = FALSE
        WHERE instance_id = 'inst-unified-capacity-accounting'
          AND instance_generation = 1
        """
    )
    db.execute(
        """
        UPDATE host_nodes
        SET reserved_resident_unified_memory_bytes = 100,
            reserved_incremental_unified_memory_bytes = 1,
            reserved_cpu_decode_millicores = 1,
            reserved_host_pcm_buffer_bytes = 1,
            reserved_scratch_disk_bytes = 1
        WHERE host_node_id = 'node-unified-capacity-accounting'
        """
    )
    db.execute(
        """
        UPDATE engine_instances
        SET reserved_active_slots = 1
        WHERE instance_id = 'inst-unified-capacity-accounting'
          AND instance_generation = 1
        """
    )
    db.commit()

    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'quarantined',
            cleanup_state = 'quarantined',
            quarantined_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-unified-capacity-accounting'
        """
    )
    db.execute(
        """
        UPDATE engine_instances
        SET instance_state = 'quarantined',
            quarantine_reason = 'synthetic unknown execution',
            reserved_active_slots = 0,
            quarantined_slots = 1
        WHERE instance_id = 'inst-unified-capacity-accounting'
          AND instance_generation = 1
        """
    )
    db.execute(
        """
        UPDATE host_nodes
        SET reserved_resident_unified_memory_bytes = 0,
            reserved_incremental_unified_memory_bytes = 0,
            reserved_cpu_decode_millicores = 0,
            reserved_host_pcm_buffer_bytes = 0,
            reserved_scratch_disk_bytes = 0,
            quarantined_unified_memory_bytes = 101,
            quarantined_cpu_decode_millicores = 1,
            quarantined_host_pcm_buffer_bytes = 1,
            quarantined_scratch_disk_bytes = 1
        WHERE host_node_id = 'node-unified-capacity-accounting'
        """
    )
    db.commit()

    db.execute(
        """
        UPDATE host_nodes
        SET quarantined_unified_memory_bytes = 100
        WHERE host_node_id = 'node-unified-capacity-accounting'
        """
    )
    with pytest.raises(CheckViolation):
        db.commit()
    db.rollback()


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

    observed_at = "2026-10-23 00:00:00+00"
    db.execute(
        """
        UPDATE admission_leases
        SET quarantine_release_evidence_kind = 'operator_verified_instance_terminated',
            quarantine_release_evidence_digest = %s,
            quarantine_release_observed_at = %s,
            quarantine_release_reason = 'operator confirmed the old container exited'
        WHERE lease_id = 'lease-release'
        """,
        (_DIGEST, observed_at),
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET quarantine_release_evidence_digest = %s
            WHERE lease_id = 'lease-release'
            """,
            ("sha256:" + "b" * 64,),
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET quarantined_at = CURRENT_TIMESTAMP + INTERVAL '1 hour'
            WHERE lease_id = 'lease-release'
            """
        )

    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'released',
            cleanup_state = 'complete',
            quarantine_release_evidence_kind = 'operator_verified_instance_terminated',
            quarantine_release_evidence_digest = %s,
            quarantine_release_observed_at = %s,
            quarantine_release_reason = 'operator confirmed the old container exited',
            budget_released_at = %s
        WHERE lease_id = 'lease-release'
        """,
        (_DIGEST, observed_at, observed_at),
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
            dispatched=suffix == "success",
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
            SET execution_state = 'executing'
            WHERE submit_intent_id = 'intent-cleanup-failure'
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
    db.execute(
        """
        UPDATE submit_intents
        SET execution_state = 'executing',
            dispatched_at = CURRENT_TIMESTAMP,
            engine_request_id = 'engine-request-cleanup-failure'
        WHERE submit_intent_id = 'intent-cleanup-failure'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET dispatched_at = CURRENT_TIMESTAMP + INTERVAL '1 second'
            WHERE submit_intent_id = 'intent-cleanup-failure'
            """
        )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET execution_state = 'failed',
                cleanup_state = 'not_required',
                stop_evidence_kind = 'none',
                error_code = 'policy_or_resource_disabled'
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


def test_active_submit_intent_cannot_claim_terminal_evidence(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-active-evidence")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-active-evidence",
        generation=1,
        host_node_id="node-active-evidence",
        architecture="discrete_device_memory",
    )
    _insert_lease(
        db,
        lease_id="lease-active-evidence",
        operation_id="op-active-evidence",
        attempt_id="att-active-evidence",
        host_node_id="node-active-evidence",
        instance_id="inst-active-evidence",
        generation=1,
    )
    _insert_submit_intent(
        db,
        submit_intent_id="intent-active-evidence",
        lease_id="lease-active-evidence",
        operation_id="op-active-evidence",
        attempt_id="att-active-evidence",
        host_node_id="node-active-evidence",
        instance_id="inst-active-evidence",
        generation=1,
        dispatched=False,
    )

    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET stop_evidence_kind = 'engine_abort_ack'
            WHERE submit_intent_id = 'intent-active-evidence'
            """
        )
    db.execute(
        """
        UPDATE submit_intents
        SET execution_state = 'failed',
            cleanup_state = 'not_required',
            error_code = 'policy_denied_before_dispatch'
        WHERE submit_intent_id = 'intent-active-evidence'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE submit_intents
            SET error_code = 'different_terminal_error'
            WHERE submit_intent_id = 'intent-active-evidence'
            """
        )


def test_lease_release_waits_for_terminal_submit_intents(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-lease-release")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-lease-release",
        generation=1,
        host_node_id="node-lease-release",
        architecture="discrete_device_memory",
    )
    _insert_lease(
        db,
        lease_id="lease-active-release",
        operation_id="op-active-release",
        attempt_id="att-active-release",
        host_node_id="node-lease-release",
        instance_id="inst-lease-release",
        generation=1,
    )
    _insert_submit_intent(
        db,
        submit_intent_id="intent-active-release",
        lease_id="lease-active-release",
        operation_id="op-active-release",
        attempt_id="att-active-release",
        host_node_id="node-lease-release",
        instance_id="inst-lease-release",
        generation=1,
        dispatched=False,
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET lease_state = 'released',
                cleanup_state = 'not_required',
                budget_released_at = CURRENT_TIMESTAMP
            WHERE lease_id = 'lease-active-release'
            """
        )
    db.execute(
        """
        UPDATE submit_intents
        SET execution_state = 'failed',
            cleanup_state = 'not_required',
            error_code = 'policy_denied_before_dispatch'
        WHERE submit_intent_id = 'intent-active-release'
        """
    )
    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'released',
            cleanup_state = 'not_required',
            budget_released_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-active-release'
        """
    )

    _insert_lease(
        db,
        lease_id="lease-unknown-release",
        operation_id="op-unknown-release",
        attempt_id="att-unknown-release",
        host_node_id="node-lease-release",
        instance_id="inst-lease-release",
        generation=1,
    )
    _insert_submit_intent(
        db,
        submit_intent_id="intent-unknown-release",
        lease_id="lease-unknown-release",
        operation_id="op-unknown-release",
        attempt_id="att-unknown-release",
        host_node_id="node-lease-release",
        instance_id="inst-lease-release",
        generation=1,
    )
    db.execute(
        """
        UPDATE submit_intents
        SET execution_state = 'unknown_quarantined',
            cleanup_state = 'quarantined',
            stop_evidence_kind = 'unconfirmed_timeout'
        WHERE submit_intent_id = 'intent-unknown-release'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE admission_leases
            SET lease_state = 'released',
                cleanup_state = 'complete',
                budget_released_at = CURRENT_TIMESTAMP
            WHERE lease_id = 'lease-unknown-release'
            """
        )
    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'quarantined',
            cleanup_state = 'quarantined',
            quarantined_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-unknown-release'
        """
    )
    db.execute(
        """
        UPDATE submit_intents
        SET execution_state = 'aborted',
            cleanup_state = 'complete',
            stop_evidence_kind = 'engine_terminal_response',
            terminated_at = CURRENT_TIMESTAMP
        WHERE submit_intent_id = 'intent-unknown-release'
        """
    )
    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'released',
            cleanup_state = 'complete',
            quarantine_release_evidence_kind = 'engine_terminal_response',
            quarantine_release_evidence_digest = %s,
            quarantine_release_observed_at = CURRENT_TIMESTAMP,
            budget_released_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-unknown-release'
        """,
        (_DIGEST,),
    )
    row = db.execute(
        """
        SELECT lease_state, cleanup_state
        FROM admission_leases
        WHERE lease_id = 'lease-unknown-release'
        """
    ).fetchone()
    assert row == ("released", "complete")


def test_submit_intent_requires_a_live_admission_lease(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-live-lease")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-live-lease",
        generation=1,
        host_node_id="node-live-lease",
        architecture="discrete_device_memory",
    )
    _insert_lease(
        db,
        lease_id="lease-released-before-submit",
        operation_id="op-released-before-submit",
        attempt_id="att-released-before-submit",
        host_node_id="node-live-lease",
        instance_id="inst-live-lease",
        generation=1,
    )
    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'released',
            cleanup_state = 'not_required',
            budget_released_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-released-before-submit'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-after-release",
            lease_id="lease-released-before-submit",
            operation_id="op-released-before-submit",
            attempt_id="att-released-before-submit",
            host_node_id="node-live-lease",
            instance_id="inst-live-lease",
            generation=1,
            dispatched=False,
        )

    _insert_lease(
        db,
        lease_id="lease-quarantined-before-submit",
        operation_id="op-quarantined-before-submit",
        attempt_id="att-quarantined-before-submit",
        host_node_id="node-live-lease",
        instance_id="inst-live-lease",
        generation=1,
    )
    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'quarantined',
            cleanup_state = 'quarantined',
            quarantined_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-quarantined-before-submit'
        """
    )
    with pytest.raises(CheckViolation), db.transaction():
        _insert_submit_intent(
            db,
            submit_intent_id="intent-after-quarantine",
            lease_id="lease-quarantined-before-submit",
            operation_id="op-quarantined-before-submit",
            attempt_id="att-quarantined-before-submit",
            host_node_id="node-live-lease",
            instance_id="inst-live-lease",
            generation=1,
            dispatched=False,
        )


def test_stopped_instance_requires_all_leases_released(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-stop-cleanup")
    _insert_pool(db)
    _insert_instance(
        db,
        instance_id="inst-stop-cleanup",
        generation=1,
        host_node_id="node-stop-cleanup",
        architecture="discrete_device_memory",
    )
    _insert_lease(
        db,
        lease_id="lease-stop-cleanup",
        operation_id="op-stop-cleanup",
        attempt_id="att-stop-cleanup",
        host_node_id="node-stop-cleanup",
        instance_id="inst-stop-cleanup",
        generation=1,
    )
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE engine_instances
            SET instance_state = 'stopped',
                stopped_at = CURRENT_TIMESTAMP,
                peak_reservation_active = FALSE
            WHERE instance_id = 'inst-stop-cleanup' AND instance_generation = 1
            """
        )
    db.execute(
        """
        UPDATE admission_leases
        SET lease_state = 'released',
            cleanup_state = 'not_required',
            budget_released_at = CURRENT_TIMESTAMP
        WHERE lease_id = 'lease-stop-cleanup'
        """
    )
    db.execute(
        """
        UPDATE engine_instances
        SET instance_state = 'stopped',
            stopped_at = CURRENT_TIMESTAMP,
            peak_reservation_active = FALSE
        WHERE instance_id = 'inst-stop-cleanup' AND instance_generation = 1
        """
    )
    row = db.execute(
        """
        SELECT instance_state
        FROM engine_instances
        WHERE instance_id = 'inst-stop-cleanup' AND instance_generation = 1
        """
    ).fetchone()
    assert row == ("stopped",)


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
    with pytest.raises(CheckViolation), db.transaction():
        db.execute(
            """
            UPDATE gateway_fencing_barriers
            SET barrier_sealed = FALSE
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
            per_slot_cpu_decode_millicores, per_slot_host_pcm_buffer_bytes,
            per_slot_scratch_disk_bytes, max_active_slots, last_fencing_token, ready_at
        ) VALUES (
            'inst-fencing', 1, 'node-fencing', 'discrete_device_memory',
            'gateway-1', 'asr-realtime', 'asr-quality@7', 'manifest-1',
            'template-1', %s, 'resident', 'managed', 'ready',
            0, 0, 0, 100, 100, 10, 100, 100, 10, 1, 1, 1, 4, 7, CURRENT_TIMESTAMP
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

    for table, column in (
        ("resource_pools", "pool_id"),
        ("scheduler_pool_leases", "pool_id"),
        ("gateway_fencing_barriers", "gateway_id"),
    ):
        with pytest.raises(CheckViolation, match="scope identity"), db.transaction():
            db.execute(
                sql.SQL("UPDATE {} SET {} = 'scope-renamed'").format(
                    sql.Identifier(table), sql.Identifier(column)
                )
            )

    for table in ("resource_pools", "scheduler_pool_leases", "gateway_fencing_barriers"):
        with pytest.raises(CheckViolation, match="append-only"), db.transaction():
            db.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))

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
    with pytest.raises(CheckViolation, match="append-only"), db.transaction():
        db.execute("DELETE FROM submit_intents WHERE submit_intent_id = 'intent-metering'")
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


@pytest.mark.parametrize(
    "table",
    [
        "host_nodes",
        "resource_pools",
        "scheduler_pool_leases",
        "engine_instances",
        "runtime_instance_intents",
        "admission_leases",
        "gateway_fencing_barriers",
        "submit_intents",
        "output_sample_intervals",
    ],
)
def test_history_cannot_be_truncated(db: psycopg.Connection[Any], table: str) -> None:
    with pytest.raises(CheckViolation, match="append-only"), db.transaction():
        db.execute(sql.SQL("TRUNCATE {} CASCADE").format(sql.Identifier(table)))


@pytest.mark.parametrize(
    "architecture,index",
    [
        ("unified_memory", 0),
        ("discrete_device_memory", 1),
        ("discrete_device_memory", 2),
        ("discrete_device_memory", 3),
        ("discrete_device_memory", 4),
        ("discrete_device_memory", 5),
    ],
)
def test_reservation_cannot_underreport_slot_budget(
    db: psycopg.Connection[Any],
    architecture: str,
    index: int,
) -> None:
    _insert_host(db, "node-vector", architecture=architecture)
    _insert_pool(db)
    unified = architecture == "unified_memory"
    _insert_instance(
        db,
        instance_id="inst-vector",
        generation=1,
        host_node_id="node-vector",
        architecture=architecture,
        unified=(100, 100, 10) if unified else (0, 0, 0),
        device=(0, 0, 0) if unified else (100, 100, 10),
        host=(0, 0, 0) if unified else (100, 100, 10),
    )
    vector = [20, 0, 0, 2, 2, 2] if unified else [0, 20, 20, 2, 2, 2]
    vector[index] -= 1
    with pytest.raises(CheckViolation, match="per-slot budget"), db.transaction():
        _insert_lease(
            db,
            lease_id="lease-vector",
            operation_id="op-vector",
            attempt_id="att-vector",
            host_node_id="node-vector",
            instance_id="inst-vector",
            generation=1,
            memory_architecture=architecture,
            reservation=tuple(vector),
            slots=2,
        )
    vector[index] += 1
    _insert_lease(
        db,
        lease_id="lease-vector",
        operation_id="op-vector",
        attempt_id="att-vector",
        host_node_id="node-vector",
        instance_id="inst-vector",
        generation=1,
        memory_architecture=architecture,
        reservation=tuple(vector),
        slots=2,
    )
    db.execute(
        "UPDATE engine_instances SET reserved_active_slots = 2 WHERE instance_id = 'inst-vector'"
    )
    db.execute(
        """UPDATE host_nodes SET reserved_peak_loading_unified_memory_bytes = %s,
        reserved_incremental_unified_memory_bytes = %s, reserved_device_memory_bytes = %s,
        reserved_host_memory_bytes = %s, reserved_cpu_decode_millicores = 2,
        reserved_host_pcm_buffer_bytes = 2, reserved_scratch_disk_bytes = 2
        WHERE host_node_id = 'node-vector'""",
        (100 if unified else 0, 20 if unified else 0, 0 if unified else 120, 0 if unified else 120),
    )
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_capacity_verification_is_deduplicated_and_rearms_after_flush(
    db: psycopg.Connection[Any],
) -> None:
    _insert_host(db, "node-queue")
    for _ in range(20):
        db.execute(
            "UPDATE host_nodes SET last_heartbeat_at = clock_timestamp() "
            "WHERE host_node_id = 'node-queue'"
        )
    assert db.execute(
        "SELECT count(*) FROM capacity_verification_queue WHERE transaction_id = txid_current()"
    ).fetchone() == (1,)
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert db.execute(
        "SELECT count(*) FROM capacity_verification_queue WHERE transaction_id = txid_current()"
    ).fetchone() == (0,)
    db.execute("SET CONSTRAINTS ALL DEFERRED")
    db.execute(
        "UPDATE host_nodes SET reserved_cpu_decode_millicores = 1 WHERE host_node_id = 'node-queue'"
    )
    with pytest.raises(CheckViolation, match="capacity counters"):
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    db.rollback()


@pytest.mark.parametrize("plane", ["speech", "gateway_control"])
def test_runtime_roles_cannot_erase_evidence_or_bypass_verification(
    db: psycopg.Connection[Any],
    plane: str,
) -> None:
    from psycopg.errors import InsufficientPrivilege

    role = "sn_test_" + uuid.uuid4().hex[:12]
    db.execute(
        sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB").format(
            sql.Identifier(role)
        )
    )
    db.execute("SELECT configure_coordination_runtime_role(%s::name, %s)", (role, plane))
    _insert_host(db, "node-role")
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    try:
        assert db.execute("SELECT count(*) FROM host_nodes").fetchone() is not None
        for statement in (
            "DELETE FROM submit_intents",
            "TRUNCATE gateway_fencing_barriers CASCADE",
            "DELETE FROM capacity_verification_queue",
            "ALTER TABLE submit_intents DISABLE TRIGGER ALL",
            "CREATE TABLE unauthorized (id integer)",
            "SELECT configure_coordination_runtime_role(current_user::name, 'speech')",
        ):
            with pytest.raises(InsufficientPrivilege), db.transaction():
                db.execute(statement)
        if plane == "speech":
            db.execute("CREATE TEMP TABLE capacity_verification_queue (spoof integer)")
            db.execute(
                "UPDATE host_nodes SET last_heartbeat_at = clock_timestamp() "
                "WHERE host_node_id = 'node-role'"
            )
            with pytest.raises(CheckViolation, match="capacity counters"), db.transaction():
                db.execute(
                    "UPDATE host_nodes SET reserved_cpu_decode_millicores = 1 "
                    "WHERE host_node_id = 'node-role'"
                )
    finally:
        db.execute("RESET ROLE")


@pytest.mark.parametrize("plane", ["speech", "gateway_control"])
@pytest.mark.parametrize(
    "trigger_function", ["enqueue_capacity_verification", "verify_queued_capacity"]
)
def test_capacity_trigger_functions_reject_untrusted_attachment_before_type_conversion(
    db: psycopg.Connection[Any], plane: str, trigger_function: str
) -> None:
    from psycopg.errors import InsufficientPrivilege

    role = "sn_test_" + uuid.uuid4().hex[:12]
    db.execute(
        sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB").format(
            sql.Identifier(role)
        )
    )
    db.execute("SELECT configure_coordination_runtime_role(%s::name, %s)", (role, plane))
    probe_table = (
        "host_nodes"
        if trigger_function == "enqueue_capacity_verification"
        else "capacity_verification_queue"
    )
    db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    try:
        db.execute("CREATE TEMP TABLE capacity_trigger_probe_init (id integer)")
        db.execute("CREATE TYPE pg_temp.capacity_probe_type AS ENUM ('probe')")
        # A harmless sentinel detects any caller-controlled conversion under the
        # function owner's identity. No privileged action or persistent data.
        db.execute("""
            CREATE FUNCTION pg_temp.capacity_probe_cast(pg_temp.capacity_probe_type)
            RETURNS TEXT LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'untrusted type conversion executed as %', current_user;
            END;
            $$
        """)
        db.execute("""
            CREATE CAST (pg_temp.capacity_probe_type AS TEXT)
            WITH FUNCTION pg_temp.capacity_probe_cast(pg_temp.capacity_probe_type) AS IMPLICIT
        """)
        db.execute(
            sql.SQL("""
            CREATE TEMP TABLE {} (
                transaction_id BIGINT, target_kind TEXT,
                target_id pg_temp.capacity_probe_type, generation INTEGER,
                host_node_id pg_temp.capacity_probe_type
            )
        """).format(sql.Identifier(probe_table))
        )
        attach = sql.SQL(
            "CREATE TRIGGER capacity_probe AFTER INSERT ON pg_temp.{} "
            "FOR EACH ROW EXECUTE FUNCTION {}()"
        ).format(sql.Identifier(probe_table), sql.Identifier(trigger_function))
        with pytest.raises(InsufficientPrivilege), db.transaction():
            db.execute(attach)

        # Even an accidental future EXECUTE grant must not expose NEW fields
        # from an untrusted table to SECURITY DEFINER expression evaluation.
        db.execute("RESET ROLE")
        db.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION {}() TO {}").format(
                sql.Identifier(trigger_function), sql.Identifier(role)
            )
        )
        db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        db.execute(attach)
        with (
            pytest.raises(InsufficientPrivilege, match="untrusted trigger source"),
            db.transaction(),
        ):
            db.execute(
                sql.SQL("""
                INSERT INTO pg_temp.{}
                VALUES (txid_current(), 'host', 'probe', 0, 'probe')
            """).format(sql.Identifier(probe_table))
            )
    finally:
        db.execute("RESET ROLE")


def _lock_race_scope(
    connection: psycopg.Connection[Any], pool: str, node: str, instance: str
) -> None:
    connection.execute("SELECT 1 FROM resource_pools WHERE pool_id = %s FOR UPDATE", (pool,))
    connection.execute("SELECT 1 FROM scheduler_pool_leases WHERE pool_id = %s FOR UPDATE", (pool,))
    connection.execute("SELECT 1 FROM host_nodes WHERE host_node_id = %s FOR UPDATE", (node,))
    connection.execute(
        "SELECT 1 FROM engine_instances WHERE instance_id = %s "
        "ORDER BY instance_generation FOR UPDATE",
        (instance,),
    )
    connection.execute(
        "SELECT 1 FROM admission_leases WHERE pool_id = %s ORDER BY lease_id FOR UPDATE", (pool,)
    )
    connection.execute(
        "SELECT 1 FROM gateway_fencing_barriers WHERE pool_id = %s ORDER BY gateway_id FOR UPDATE",
        (pool,),
    )


@pytest.mark.parametrize(
    "scenario", ["last_slot", "submit_release", "admission_drain", "takeover_submit"]
)
@pytest.mark.parametrize("reverse", [False, True])
def test_ordered_transaction_recipes_under_actual_lock_contention(
    coordination_schema: CoordinationDatabase,
    scenario: str,
    reverse: bool,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from threading import Event
    from time import monotonic

    suffix = uuid.uuid4().hex[:10]
    pool, node, instance = "pool-" + suffix, "node-" + suffix, "inst-" + suffix
    initial_slots = (
        3
        if scenario == "last_slot"
        else 1
        if scenario in ("submit_release", "takeover_submit")
        else 0
    )

    def connect() -> psycopg.Connection[Any]:
        connection = psycopg.connect(coordination_schema.url)
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(
                sql.Identifier(coordination_schema.schema)
            )
        )
        connection.execute("SET lock_timeout = '5s'")
        connection.execute("SET statement_timeout = '5s'")
        return connection

    with connect() as setup:
        _insert_host(setup, node)
        _insert_pool(setup, pool_id=pool)
        _insert_instance(
            setup,
            instance_id=instance,
            generation=1,
            host_node_id=node,
            architecture="discrete_device_memory",
            pool_id=pool,
        )
        setup.execute(
            "UPDATE resource_pools SET high_water_fencing_token = 1 WHERE pool_id = %s", (pool,)
        )
        setup.execute(
            """INSERT INTO scheduler_pool_leases VALUES
            (%s, 'owner-1', 1, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
             CURRENT_TIMESTAMP + INTERVAL '1 hour', TRUE)""",
            (pool,),
        )
        setup.execute(
            """INSERT INTO gateway_fencing_barriers
            (gateway_id,pool_id,host_node_id,enforced_min_fencing_token,new_owner_scheduler_id)
            VALUES ('gateway-1',%s,%s,1,'owner-1')""",
            (pool, node),
        )
        if initial_slots:
            _insert_lease(
                setup,
                lease_id="lease-" + suffix,
                operation_id="op-" + suffix,
                attempt_id="att-" + suffix,
                host_node_id=node,
                instance_id=instance,
                generation=1,
                pool_id=pool,
                slots=initial_slots,
                reservation=(
                    0,
                    initial_slots,
                    initial_slots,
                    initial_slots,
                    initial_slots,
                    initial_slots,
                ),
            )
        setup.execute(
            "UPDATE engine_instances SET reserved_active_slots = %s WHERE instance_id = %s",
            (initial_slots, instance),
        )
        setup.execute(
            """UPDATE host_nodes SET reserved_device_memory_bytes = %s,
            reserved_host_memory_bytes = %s, reserved_cpu_decode_millicores = %s,
            reserved_host_pcm_buffer_bytes = %s, reserved_scratch_disk_bytes = %s
            WHERE host_node_id = %s""",
            (
                100 + initial_slots,
                100 + initial_slots,
                initial_slots,
                initial_slots,
                initial_slots,
                node,
            ),
        )

    actions = {
        "last_slot": ("admit-a", "admit-b"),
        "submit_release": ("submit", "release"),
        "admission_drain": ("admit-a", "drain"),
        "takeover_submit": ("takeover", "submit"),
    }[scenario]
    if reverse:
        actions = actions[::-1]

    def act(connection: psycopg.Connection[Any], action: str) -> None:
        if action.startswith("admit"):
            _insert_lease(
                connection,
                lease_id=action + suffix,
                operation_id="op-" + action + suffix,
                attempt_id="att-" + action + suffix,
                host_node_id=node,
                instance_id=instance,
                generation=1,
                pool_id=pool,
            )
            delta = 1
        elif action == "release":
            connection.execute(
                """UPDATE admission_leases SET lease_state='released',
                cleanup_state='not_required', budget_released_at=CURRENT_TIMESTAMP
                WHERE lease_id=%s""",
                ("lease-" + suffix,),
            )
            delta = -1
        else:
            delta = 0
        if delta:
            connection.execute(
                "UPDATE engine_instances SET reserved_active_slots=reserved_active_slots+%s "
                "WHERE instance_id=%s",
                (delta, instance),
            )
            connection.execute(
                """UPDATE host_nodes SET
                reserved_device_memory_bytes=reserved_device_memory_bytes+%s,
                reserved_host_memory_bytes=reserved_host_memory_bytes+%s,
                reserved_cpu_decode_millicores=reserved_cpu_decode_millicores+%s,
                reserved_host_pcm_buffer_bytes=reserved_host_pcm_buffer_bytes+%s,
                reserved_scratch_disk_bytes=reserved_scratch_disk_bytes+%s WHERE host_node_id=%s""",
                (delta, delta, delta, delta, delta, node),
            )
        if action == "drain":
            connection.execute(
                "UPDATE engine_instances SET instance_state='draining' WHERE instance_id=%s",
                (instance,),
            )
        if action == "takeover":
            connection.execute(
                "UPDATE resource_pools SET high_water_fencing_token=2 WHERE pool_id=%s", (pool,)
            )
            connection.execute(
                """UPDATE scheduler_pool_leases SET owner_scheduler_id='owner-2',
                fencing_token=2,lease_state='reconciling_takeover',previous_owner_reconciled=FALSE
                WHERE pool_id=%s""",
                (pool,),
            )
            connection.execute(
                "UPDATE gateway_fencing_barriers SET enforced_min_fencing_token=2,"
                "new_owner_scheduler_id='owner-2' WHERE pool_id=%s",
                (pool,),
            )
        if action == "submit":
            token = connection.execute(
                "SELECT enforced_min_fencing_token FROM gateway_fencing_barriers WHERE pool_id=%s",
                (pool,),
            ).fetchone()
            assert token is not None
            if token[0] > 1:
                raise CheckViolation("gateway_barrier_fenced")
            _insert_submit_intent(
                connection,
                submit_intent_id="submit-" + suffix,
                lease_id="lease-" + suffix,
                operation_id="op-" + suffix,
                attempt_id="att-" + suffix,
                host_node_id=node,
                instance_id=instance,
                generation=1,
                pool_id=pool,
            )

    pids: Queue[int] = Queue()

    def contender() -> str:
        try:
            with connect() as connection:
                pids.put(connection.info.backend_pid)
                _lock_race_scope(connection, pool, node, instance)
                act(connection, actions[1])
            return "committed"
        except CheckViolation:
            return "rejected"

    with connect() as first, connect() as observer, ThreadPoolExecutor(max_workers=1) as executor:
        _lock_race_scope(first, pool, node, instance)
        future = executor.submit(contender)
        second_pid = pids.get(timeout=5)
        deadline = monotonic() + 5
        while True:
            blockers = observer.execute("SELECT pg_blocking_pids(%s)", (second_pid,)).fetchone()
            if blockers and first.info.backend_pid in blockers[0]:
                break
            assert monotonic() < deadline, "second transaction never reached the contested lock"
            Event().wait(0.01)
        act(first, actions[0])
        first.commit()
        outcome = future.result(timeout=5)
        expected_rejection = (
            scenario in ("last_slot", "submit_release")
            or (scenario == "admission_drain" and reverse)
            or (scenario == "takeover_submit" and not reverse)
        )
        assert outcome == ("rejected" if expected_rejection else "committed")
        # Invariants also execute at commit; neither a partial admission nor a
        # false release can survive the competing transactions.
        assert observer.execute("SELECT count(*) FROM capacity_verification_queue").fetchone() == (
            0,
        )
