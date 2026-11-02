"""A-side rejection and settlement only; synthetic rows never prove engine stop."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command as migration
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_binding_lifecycle_pg import Sandbox
from tests.integration.test_binding_lifecycle_pg import sandbox as sandbox
from tests.integration.test_coordination_foundation import Foundation, seed
from tests.integration.test_lease_release import command, stopped
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_leases import LEASE_ID, LeaseDatabase

from speechnexus_backend.speech.domain.leases import LeaseRejectedError
from speechnexus_backend.speech.infrastructure import lease_release_sql
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_settlement_grants,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.execution_release_schema import release_guard_grants
from speechnexus_backend.speech.infrastructure.jobs_postgres import PostgresJobTransactions
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.leases_sql import _update as update_projection
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool

pytestmark = pytest.mark.integration


@pytest.fixture
def lease(sandbox: Sandbox) -> Iterator[LeaseDatabase]:
    """Explicit PG-only fixture; it does not weaken the shared dual-DB gate."""
    with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        gateway_role = sandbox.schema + "_gateway"
        admin.execute(f'CREATE ROLE "{gateway_role}" NOLOGIN')
        db = JobDatabase(
            "postgresql",
            sandbox.url,
            sandbox.schema,
            sandbox.role,
            admin,
            PostgresJobTransactions(sandbox.pool),
            sandbox.pool,
        )
        try:
            for grant in coordination_runtime_grants(
                sandbox.schema, speech_role=sandbox.role, gateway_role=gateway_role
            ):
                admin.execute(grant)
            seed(db)
            yield LeaseDatabase(
                Foundation(db, sandbox.role, gateway_role),
                PostgresLeaseTransactions(sandbox.pool),
                sandbox.pool,
            )
        finally:
            admin.execute(f'DROP OWNED BY "{gateway_role}"')
            admin.execute(f'DROP ROLE "{gateway_role}"')


@pytest.mark.parametrize(
    "assignment",
    [
        "engine_request_id = 'file-asr-synthetic'",
        "dispatched_at_us = 1",
        "staging_output_object_key = 'synthetic/staging/result'",
        "staging_output_object_key = 'synthetic/staging/result', staging_result_document = '{}'",
        "generated_pcm_samples = 1",
    ],
)
def test_accepted_or_output_history_cannot_be_disguised_as_no_dispatch(
    lease: LeaseDatabase,
    assignment: str,
) -> None:
    db = lease.foundation.database
    # Deliberately inconsistent legacy/buggy writer state. Never a stop producer.
    db.query(
        "UPDATE submit_intents SET execution_state='failed', cleanup_state='not_required', "
        + assignment
    )
    before = lease.snapshot()
    result = lease.port.release_lease(command(lease))
    assert result.outcome == "rejected"
    assert result.error_code == "execution_cleanup_pending"
    assert lease.snapshot() == before


def test_dispatch_history_committed_during_real_submit_wait_is_rechecked(
    lease: LeaseDatabase,
) -> None:
    db = lease.foundation.database
    db.query("UPDATE submit_intents SET execution_state='failed', cleanup_state='not_required'")
    original = command(lease)
    with db.blocker() as blocker:
        blocker.execute("SELECT submit_intent_id FROM submit_intents FOR UPDATE").fetchall()
        blocker.execute("UPDATE submit_intents SET engine_request_id='file-asr-synthetic'")
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(lease.port.release_lease, original)
            try:
                lease.wait_for_lock()
            finally:
                blocker.commit()
            result = future.result(timeout=5)
    assert result.error_code == "execution_cleanup_pending"
    assert db.query("SELECT lease_state FROM admission_leases WHERE lease_id=%s", (LEASE_ID,)) == [
        {"lease_state": "reserved"}
    ]
    assert db.query("SELECT held_attempts FROM workspace_quotas") == [{"held_attempts": 1}]


def no_dispatch_failure(lease: LeaseDatabase) -> None:
    lease.foundation.database.query(
        "UPDATE submit_intents SET execution_state='failed', cleanup_state='not_required'"
    )


def test_output_interval_with_stale_zero_aggregate_still_retains_budget(
    lease: LeaseDatabase,
) -> None:
    no_dispatch_failure(lease)
    db = lease.foundation.database
    db.query(
        "INSERT INTO output_sample_intervals (interval_id, submit_intent_id, operation_id, "
        "attempt_id, output_stream_id, meter_kind, sample_start, sample_end) VALUES "
        "('interval-synthetic','submit-test','operation-test','attempt-test','tts-main',"
        "'generated',0,1)"
    )
    before = lease.snapshot((*lease.snapshot().keys(), "output_sample_intervals"))
    assert lease.port.release_lease(command(lease)).error_code == "execution_cleanup_pending"
    assert lease.snapshot(tuple(before)) == before


@pytest.mark.parametrize("forged_stop", [False, True])
def test_silent_success_and_self_asserted_zero_stop_cannot_release(
    lease: LeaseDatabase,
    forged_stop: bool,
) -> None:
    db = lease.foundation.database
    db.query(
        "UPDATE submit_intents SET execution_state='succeeded', cleanup_state='complete', "
        "engine_request_id='file-asr-synthetic', dispatched_at_us=1, "
        "staging_output_object_key='synthetic/result', staging_result_document='{}'"
    )
    if forged_stop:
        # Admin-only adversarial fixture: a data shape and true flags are not authority.
        db.query(
            "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, evidence_kind, "
            "evidence_digest, observed_at_us, execution_stopped, resources_released) VALUES "
            "('synthetic-zero','submit-test','zero_dispatch_sealed',%s,1,1,1)",
            ("sha256:" + "e" * 64,),
        )
    before = lease.snapshot()
    result = lease.port.release_lease(command(lease))
    assert result.outcome == "rejected"
    assert result.error_code == (
        "reconciliation_required" if forged_stop else "execution_cleanup_pending"
    )
    assert lease.snapshot() == before


def test_original_lease_replay_after_actual_commit_lost_ack(lease: LeaseDatabase) -> None:
    no_dispatch_failure(lease)
    # Only one unit belongs to this execution; seven remain as the synthetic
    # running instance baseline. This test does not stop a real instance.
    db = lease.foundation.database
    db.query("UPDATE host_nodes SET held_unified_physical_memory_bytes=8")
    original = command(lease)
    calls = 0

    class LostAcknowledgement:
        @contextmanager
        def connection(self, **kwargs: Any) -> Iterator[Any]:
            nonlocal calls
            calls += 1
            assert lease.pool is not None
            with lease.pool.connection(**kwargs) as connection:
                yield connection
            # The real connection context has COMMITTED before the injected loss.
            raise psycopg.OperationalError("synthetic commit acknowledgement loss")

    port = PostgresLeaseTransactions(cast(SpeechPool, LostAcknowledgement()))
    result = port.release_lease(original)
    assert result.outcome == "unknown" and calls == 1
    fact = lease.port.query_lease(original.lease_id).fact
    assert fact is not None and fact.lease_state == "released"
    before = lease.snapshot()
    for replay in (original, replace(original, expected_version=999)):
        result = lease.port.release_lease(replay)
        assert result.outcome == "replayed" and result.fact == fact
    assert lease.snapshot() == before
    assert db.query("SELECT held_unified_physical_memory_bytes AS held FROM host_nodes") == [
        {"held": 7}
    ]
    assert db.query("SELECT instance_state FROM engine_instances") == [{"instance_state": "ready"}]


def test_mid_transaction_failure_rolls_back_all_accounting(
    lease: LeaseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_dispatch_failure(lease)
    original, before = command(lease), lease.snapshot()
    update = update_projection

    def fail_after_host(cursor: Any, query: str, parameters: tuple[object, ...]) -> None:
        update(cursor, query, parameters)
        if query.startswith("UPDATE host_nodes"):
            raise LeaseRejectedError("reconciliation_required")

    monkeypatch.setattr(lease_release_sql, "_update", fail_after_host)
    assert lease.port.release_lease(original).outcome == "rejected"
    assert lease.snapshot() == before
    monkeypatch.setattr(lease_release_sql, "_update", update)
    assert lease.port.release_lease(original).outcome == "committed"


def test_two_connections_settle_original_lease_once_after_pool_wait(lease: LeaseDatabase) -> None:
    no_dispatch_failure(lease)
    db, original = lease.foundation.database, command(lease)
    with db.blocker() as blocker:
        blocker.execute("SELECT pool_id FROM resource_pools FOR UPDATE").fetchall()
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(lease.port.release_lease, original) for _ in range(2)]
            try:
                lease.wait_for_lock(2)
            finally:
                blocker.commit()
            results = [future.result(timeout=5) for future in futures]
    assert sorted(result.outcome for result in results) == ["committed", "replayed"]
    assert results[0].fact == results[1].fact
    assert db.query("SELECT held_attempts FROM workspace_quotas") == [{"held_attempts": 0}]
    assert db.query("SELECT version FROM admission_leases") == [{"version": 2}]


@pytest.mark.parametrize("missing", ["view", "grant"])
def test_missing_guard_dependency_rejects_without_releasing(
    lease: LeaseDatabase, missing: str
) -> None:
    no_dispatch_failure(lease)
    db, before = lease.foundation.database, lease.snapshot()
    if missing == "view":
        db.query("DROP VIEW execution_release_guards")
    else:
        db.query(
            "REVOKE SELECT (submit_intent_id, no_dispatch_metadata_absent) "
            f'ON execution_release_guards FROM "{lease.foundation.speech_role}"'
        )
    assert lease.port.release_lease(command(lease)).error_code == "reconciliation_required"
    assert lease.snapshot() == before


def test_presence_projection_does_not_grant_private_data_or_evidence_writes(
    lease: LeaseDatabase,
) -> None:
    assert lease.pool is not None
    with lease.pool.connection() as connection:
        rows = connection.execute("SELECT * FROM execution_release_guards").fetchall()
        assert rows == [{"submit_intent_id": "submit-test", "no_dispatch_metadata_absent": 1}]
    for query in (
        "SELECT engine_request_id FROM submit_intents",
        "SELECT staging_result_document FROM submit_intents",
        "SELECT staging_output_object_key FROM submit_intents",
        "SELECT request_document FROM submit_intents",
        "UPDATE execution_release_guards SET submit_intent_id='other'",
        "DELETE FROM execution_release_guards",
        "DELETE FROM execution_stop_evidence",
        "INSERT INTO execution_stop_evidence SELECT * FROM execution_stop_evidence",
    ):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            lease.pool.connection() as connection,
        ):
            connection.execute(query)


def test_existing_stop_release_does_not_depend_on_no_dispatch_projection(
    lease: LeaseDatabase,
) -> None:
    # Existing synthetic engine fact tests only the consumer; no physical stop claim.
    stopped(lease)
    lease.foundation.database.query("DROP VIEW execution_release_guards")
    assert lease.port.release_lease(command(lease)).outcome == "committed"


def test_separate_disable_settlement_role_can_consume_only_the_presence_projection(
    lease: LeaseDatabase,
) -> None:
    no_dispatch_failure(lease)
    db = lease.foundation.database
    role = db.schema + "_settlement"
    db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    pool = speech_pool(
        make_conninfo(db.url, user=role, password="synthetic_settlement_test"),
        schema=db.schema,
    )
    try:
        for grant in disable_settlement_grants(db.schema, role):
            db.query(grant)
        pool.open()
        for field in ("engine_request_id", "staging_result_document", "request_document"):
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                pool.connection() as connection,
            ):
                connection.execute(f"SELECT {field} FROM submit_intents")
        assert PostgresLeaseTransactions(pool).release_lease(command(lease)).outcome == "committed"
    finally:
        pool.close()
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE "{role}"')


def test_view_upgrade_and_downgrade_keep_source_history_and_existing_grants(
    lease: LeaseDatabase,
) -> None:
    no_dispatch_failure(lease)
    db, before = lease.foundation.database, lease.snapshot()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["schema"] = db.schema
    engine = create_engine(
        db.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.downgrade(config, "0027_gateway_abort_intents")
        assert lease.snapshot() == before
        assert lease.port.release_lease(command(lease)).error_code == "reconciliation_required"
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
            migration.upgrade(config, "head")
        assert lease.snapshot() == before
        # A new view requires an explicit owner grant; startup never does this.
        assert lease.port.release_lease(command(lease)).error_code == "reconciliation_required"
        for grant in release_guard_grants(db.schema, lease.foundation.speech_role):
            db.query(grant)
        assert lease.port.release_lease(command(lease)).outcome == "committed"
    finally:
        engine.dispose()
