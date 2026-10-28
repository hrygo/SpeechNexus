"""Upgrade old and new 0023 installations without losing disable roots."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_jobs import database as database

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
MigrationSandbox = tuple[Config, psycopg.Connection[tuple[object, ...]]]


@pytest.fixture
def migration_sandbox() -> Iterator[tuple[Config, psycopg.Connection[tuple[object, ...]]]]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Configure the isolated PostgreSQL test database")
    schema = "sn_disable_migration_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    admin = psycopg.connect(url, autocommit=True)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0024_binding_publish_intents")
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        yield config, admin
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def seed_disable(admin: psycopg.Connection[tuple[object, ...]], *, matching: bool = True) -> None:
    admin.execute(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('binding','binding-test','1','revision-test',1)"
    )
    admin.execute(
        "INSERT INTO speech_publication_retirements "
        "(kind,object_id,revision,revision_id,retired_at_us,mode,reason) "
        "VALUES ('binding','binding-test','1','revision-test',2,'disable','synthetic')"
    )
    admin.execute(
        "INSERT INTO speech_publication_lifecycle_events "
        "(event_id,kind,action,binding_id,revision,revision_id,result_status,retired_at_us,"
        "disabled_reason,request_id,actor_type,occurred_at_us,policy_revision,intent_digest,"
        "change_summary_json) VALUES (%s,'binding','model_binding.disable','binding-test','1',"
        "'revision-test','disabled',2,%s,'request-test','anonymous',2,1,%s,'{}'::jsonb)",
        ("event-test", "synthetic" if matching else "different", "sha256:" + "a" * 64),
    )


def upgrade(config: Config) -> None:
    engine = create_engine(
        os.environ["SPEECHNEXUS_TEST_DATABASE_URL"].replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            command.upgrade(config, "head")
    finally:
        engine.dispose()


@pytest.mark.parametrize("old_installation", [True, False])
def test_upgrade_backfills_old_and_new_disable_roots(
    migration_sandbox: tuple[Config, psycopg.Connection[tuple[object, ...]]], old_installation: bool
) -> None:
    config, admin = migration_sandbox
    if old_installation:
        # The earlier 0023 schema has no root table; this isolated table is empty.
        admin.execute("DROP TABLE speech_binding_disable_intents")
    seed_disable(admin)
    admin.execute(
        "INSERT INTO speech_publication_lifecycle_events "
        "(event_id,kind,action,binding_id,revision,revision_id,result_status,request_id,"
        "actor_type,occurred_at_us,policy_revision,intent_digest,change_summary_json) "
        "VALUES ('event-other','binding','model_binding.activate','binding-test','1',"
        "'revision-test','published','request-other','anonymous',3,1,%s,'{}'::jsonb)",
        ("sha256:" + "b" * 64,),
    )
    if not old_installation:
        admin.execute("INSERT INTO speech_binding_disable_intents VALUES ('event-test')")
    upgrade(config)
    assert admin.execute("SELECT event_id FROM speech_binding_disable_intents").fetchall() == [
        ("event-test",)
    ]
    assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0027_gateway_abort_intents",
    )
    assert admin.execute("SELECT count(*) FROM speech_binding_disable_targets").fetchone() == (0,)


def test_inconsistent_history_rolls_back_created_root_and_version(
    migration_sandbox: tuple[Config, psycopg.Connection[tuple[object, ...]]],
) -> None:
    config, admin = migration_sandbox
    admin.execute("DROP TABLE speech_binding_disable_intents")
    seed_disable(admin, matching=False)
    with pytest.raises(RuntimeError, match="Historical binding disable facts"):
        upgrade(config)
    assert admin.execute("SELECT to_regclass('speech_binding_disable_intents')").fetchone() == (
        None,
    )
    assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0024_binding_publish_intents",
    )
    assert admin.execute("SELECT count(*) FROM speech_publication_lifecycle_events").fetchone() == (
        1,
    )


def test_incompatible_existing_root_fails_without_replacement(
    migration_sandbox: tuple[Config, psycopg.Connection[tuple[object, ...]]],
) -> None:
    config, admin = migration_sandbox
    admin.execute("DROP TABLE speech_binding_disable_intents")
    admin.execute("CREATE TABLE speech_binding_disable_intents (event_id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="incompatible columns"):
        upgrade(config)
    assert admin.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid='speech_binding_disable_intents'::regclass "
        "AND attname='event_id'"
    ).fetchone() == ("text",)
    assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0024_binding_publish_intents",
    )


def test_upgrade_waits_for_lifecycle_writer_before_scanning(
    migration_sandbox: tuple[Config, psycopg.Connection[tuple[object, ...]]],
) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    config, admin = migration_sandbox
    admin.execute("DROP TABLE speech_binding_disable_intents")
    schema_row = admin.execute("SELECT current_schema()").fetchone()
    assert schema_row is not None and isinstance(schema_row[0], str)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with psycopg.connect(os.environ["SPEECHNEXUS_TEST_DATABASE_URL"]) as writer:
            writer.execute(f'SET search_path TO "{schema_row[0]}", pg_catalog')
            writer.execute("LOCK TABLE speech_publication_retirements IN ROW EXCLUSIVE MODE")
            pid_row = writer.execute("SELECT pg_backend_pid()").fetchone()
            assert pid_row is not None
            future = executor.submit(upgrade, config)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                count = admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                    (pid_row[0],),
                ).fetchone()
                if count is not None and count[0]:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("migration did not wait on the lifecycle writer")
            assert not future.done()
            seed_disable(writer)
        future.result(timeout=10)
    assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0027_gateway_abort_intents",
    )
    assert admin.execute("SELECT event_id FROM speech_binding_disable_intents").fetchall() == [
        ("event-test",)
    ]


def test_root_with_incorrect_foreign_key_fails_without_replacement(
    migration_sandbox: tuple[Config, psycopg.Connection[tuple[object, ...]]],
) -> None:
    config, admin = migration_sandbox
    admin.execute("DROP TABLE speech_binding_disable_intents")
    admin.execute(
        "CREATE TABLE speech_binding_disable_intents ("
        'event_id VARCHAR(128) COLLATE "C" PRIMARY KEY)'
    )
    with pytest.raises(RuntimeError, match="incompatible constraints"):
        upgrade(config)
    assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0024_binding_publish_intents",
    )


def test_later_ddl_failure_rolls_back_root_creation_and_backfill(
    migration_sandbox: tuple[Config, psycopg.Connection[tuple[object, ...]]],
) -> None:
    from sqlalchemy.exc import ProgrammingError

    config, admin = migration_sandbox
    admin.execute("DROP TABLE speech_binding_disable_intents")
    seed_disable(admin)
    admin.execute("CREATE TABLE speech_binding_disable_targets (test_marker INTEGER)")
    with pytest.raises(ProgrammingError):
        upgrade(config)
    assert admin.execute("SELECT to_regclass('speech_binding_disable_intents')").fetchone() == (
        None,
    )
    assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0024_binding_publish_intents",
    )
    assert admin.execute("SELECT count(*) FROM speech_publication_lifecycle_events").fetchone() == (
        1,
    )


def test_empty_disable_and_gateway_ledgers_can_downgrade_only_to_0024(
    migration_sandbox: MigrationSandbox,
) -> None:
    config, admin = migration_sandbox
    upgrade(config)
    engine = create_engine(
        os.environ["SPEECHNEXUS_TEST_DATABASE_URL"].replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0024_binding_publish_intents")
        assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0024_binding_publish_intents",
        )
        assert admin.execute(
            "SELECT to_regclass('speech_binding_disable_intents') IS NOT NULL"
        ).fetchone() == (True,)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="identity require preservation"):
                command.downgrade(config, "0023_publication_lifecycle")
    finally:
        engine.dispose()


def test_empty_targets_with_disable_root_still_refuse_downgrade(
    migration_sandbox: MigrationSandbox,
) -> None:
    config, admin = migration_sandbox
    seed_disable(admin)
    upgrade(config)
    engine = create_engine(
        os.environ["SPEECHNEXUS_TEST_DATABASE_URL"].replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(
                RuntimeError, match="disable cancellation targets require preservation"
            ):
                command.downgrade(config, "0024_binding_publish_intents")
        assert admin.execute("SELECT event_id FROM speech_binding_disable_intents").fetchall() == [
            ("event-test",)
        ]
        assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0027_gateway_abort_intents",
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("starting_version", "target_version"),
    [
        ("0025_binding_disable_targets", "0024_binding_publish_intents"),
        ("0027_gateway_abort_intents", "0025_binding_disable_targets"),
    ],
)
@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_downgrade_rejects_old_snapshots_before_ddl(
    migration_sandbox: MigrationSandbox,
    starting_version: str,
    target_version: str,
    isolation: str,
) -> None:
    config, admin = migration_sandbox
    engine = create_engine(
        os.environ["SPEECHNEXUS_TEST_DATABASE_URL"].replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, starting_version)
        with engine.connect().execution_options(isolation_level=isolation) as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="READ COMMITTED"):
                command.downgrade(config, target_version)
        assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
            starting_version,
        )
    finally:
        engine.dispose()


def test_disable_downgrade_waits_for_writer_and_preserves_new_root(
    migration_sandbox: MigrationSandbox,
) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    config, admin = migration_sandbox
    upgrade(config)
    schema_row = admin.execute("SELECT current_schema()").fetchone()
    assert schema_row is not None and isinstance(schema_row[0], str)
    schema = schema_row[0]
    engine = create_engine(
        os.environ["SPEECHNEXUS_TEST_DATABASE_URL"].replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        poolclass=NullPool,
        hide_parameters=True,
    )

    def run_downgrade() -> None:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0024_binding_publish_intents")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with psycopg.connect(os.environ["SPEECHNEXUS_TEST_DATABASE_URL"]) as writer:
                writer.execute(f'SET search_path TO "{schema}", pg_catalog')
                seed_disable(writer)
                writer.execute("INSERT INTO speech_binding_disable_intents VALUES ('event-test')")
                pid_row = writer.execute("SELECT pg_backend_pid()").fetchone()
                assert pid_row is not None
                pid = pid_row[0]
                future = executor.submit(run_downgrade)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waiting = admin.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE %s=ANY(pg_blocking_pids(pid)))",
                        (pid,),
                    ).fetchone()
                    assert waiting is not None
                    if waiting[0]:
                        break
                    assert not future.done()
                    time.sleep(0.01)
                else:
                    pytest.fail("downgrade did not wait for the disable writer")
            with pytest.raises(
                RuntimeError, match="disable cancellation targets require preservation"
            ):
                future.result(timeout=10)
        assert admin.execute("SELECT event_id FROM speech_binding_disable_intents").fetchall() == [
            ("event-test",)
        ]
        assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0027_gateway_abort_intents",
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_gateway_abort_downgrade_waits_for_committed_receipt(foundation: Foundation) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    from tests.integration.test_gateway_submit_ledger import (
        identity_for,
        open_pool,
        request_for,
        seed_second_attempt,
        service_for,
    )

    from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

    seed_second_attempt(foundation)
    pool = open_pool(foundation)
    try:
        service, _ = service_for(PostgresSubmissionLedger(pool))
        service.submit(
            request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a"))
        )
    finally:
        pool.close()
    db = foundation.database
    engine = create_engine(
        db.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = db.schema

    def run_downgrade() -> None:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0025_binding_disable_targets")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with psycopg.connect(db.url) as writer:
                writer.execute(f'SET search_path TO "{db.schema}", pg_catalog')
                writer.execute(
                    "INSERT INTO gateway_abort_intents "
                    "(submit_intent_id,operation_id,attempt_id,fencing_token,reason,"
                    "wait_timeout_ms,received_at_us) "
                    "VALUES ('intent-a','operation-test','attempt-alt',1,'synthetic',100,1)"
                )
                pid_row = writer.execute("SELECT pg_backend_pid()").fetchone()
                assert pid_row is not None
                pid = pid_row[0]
                future = executor.submit(run_downgrade)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    rows = db.query(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE %s=ANY(pg_blocking_pids(pid))) AS waiting",
                        (pid,),
                    )
                    if rows[0]["waiting"]:
                        break
                    assert not future.done()
                    time.sleep(0.01)
                else:
                    pytest.fail("downgrade did not wait for the gateway abort writer")
            with pytest.raises(RuntimeError, match="Gateway abort intents require preservation"):
                future.result(timeout=10)
        assert db.query("SELECT submit_intent_id FROM gateway_abort_intents") == [
            {"submit_intent_id": "intent-a"}
        ]
        assert db.query("SELECT version_num FROM alembic_version") == [
            {"version_num": "0027_gateway_abort_intents"}
        ]
    finally:
        engine.dispose()
