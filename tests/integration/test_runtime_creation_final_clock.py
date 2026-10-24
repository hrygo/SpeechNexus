"""A rolled-back unique-key rival must not make pre-wait authority reusable."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.fake.runtime_intents import COMMAND, CONFIG, REQUEST, TOPOLOGY
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import RuntimeDatabase
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database

from speechnexus_backend.speech.domain.runtime_intents import RuntimeDeadline
from speechnexus_backend.speech.infrastructure.runtime_codec import prepare_request

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("expires", ["owner", "request"])
@pytest.mark.parametrize("wait_at", ["instance", "outbox"])
def test_rolled_back_global_identity_race_cannot_bypass_expiry(
    runtime_database: RuntimeDatabase,
    expires: str,
    wait_at: str,
) -> None:
    runtime, db = runtime_database, runtime_database.foundation.database
    # Different resource parents leave only the global instance PK in common.
    # This uncommitted relational rival never starts a runtime and rolls back.
    db.query(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES ('asr-realtime', 'realtime', 'active')"
    )
    db.query(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, topology_digest) "
        "VALUES ('host-rival', 'unified_memory', 'active', %s)",
        (TOPOLOGY,),
    )
    event_id = prepare_request(REQUEST).event_id
    expires_at = runtime.now() + 500000
    command = COMMAND
    if expires == "owner":
        db.query(
            "UPDATE resource_pools SET owner_expires_at_us = %s WHERE pool_id='tts-realtime'",
            (expires_at,),
        )
    else:
        text = (
            (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=expires_at))
            .isoformat()
            .replace("+00:00", "Z")
        )
        command = replace(COMMAND, request=replace(REQUEST, deadline=RuntimeDeadline(text)))
    snapshot = runtime.snapshot()
    rival = db.blocker()
    try:
        with rival.cursor() as cursor:
            if wait_at == "instance":
                cursor.execute(
                    "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, "
                    "pool_id, gateway_id, binding_revision_id, manifest_id, approved_template_id, "
                    "management_mode, loading_policy, config_digest, instance_state, max_slots) "
                    "VALUES ('instance-new', 1, 'host-rival', 'asr-realtime', 'gateway-test', "
                    "'binding-test', 'manifest-test', 'template-test', 'managed', 'on_demand', %s, "
                    "'preparing', 4)",
                    (CONFIG,),
                )
            else:
                cursor.execute(
                    "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, "
                    "initial_event_id, initial_event_digest, request_digest) "
                    "VALUES ('job-rival', 'operation-rival', 'attempt-rival', %s, %s, %s)",
                    (event_id, CONFIG, CONFIG),
                )
                cursor.execute(
                    "INSERT INTO coordination_outbox (event_id, job_id, event_digest) "
                    "VALUES (%s, 'job-rival', %s)",
                    (event_id, CONFIG),
                )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(runtime.port.create_runtime_intent, command)
            try:
                runtime.wait_for_lock()
                time.sleep(0.55)
                assert runtime.now() >= expires_at
            finally:
                rival.rollback()  # The competing identity vanishes, so INSERT can proceed.
            result = future.result(timeout=5)
        assert result.error_code == (
            "stale_fencing_token" if expires == "owner" else "lease_not_active"
        )
        assert runtime.snapshot() == snapshot
    finally:
        rival.rollback()
        rival.close()
