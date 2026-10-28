"""Production storage/privilege foundation; no scheduler or engine qualification."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psycopg
import pymysql
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool
from tests.integration.test_production_jobs import REQUEST, JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.support.job_database import MySQLJobTransactions
from tests.support.migrations import HEAD

from speechnexus_backend.speech.infrastructure.admission_command_schema import (
    ADMISSION_COMMAND_DDL,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    GATEWAY_UPDATES,
    SPEECH_UPDATES,
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.coordination_schema import COORDINATION_DDL
from speechnexus_backend.speech.infrastructure.execution_result_schema import EXECUTION_RESULT_DDL
from speechnexus_backend.speech.infrastructure.job_schema import job_runtime_grants
from speechnexus_backend.speech.infrastructure.jobs_sql import JobCursor, create_in_transaction
from speechnexus_backend.speech.infrastructure.runtime_delivery_schema import RUNTIME_DELIVERY_DDL
from speechnexus_backend.speech.infrastructure.runtime_intent_schema import RUNTIME_INTENT_DDL
from speechnexus_backend.speech.infrastructure.runtime_observation_schema import (
    RUNTIME_OBSERVATION_DDL,
)

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Foundation:
    database: JobDatabase = field(repr=False)
    speech_role: str
    gateway_role: str

    @contextmanager
    def connection(self, owner: str) -> Iterator[Any]:
        role = self.speech_role if owner == "speech" else self.gateway_role
        db = self.database
        connection: Any
        if db.dialect == "postgresql":
            connection = psycopg.connect(
                make_conninfo(db.url, user=role, password="synthetic_foundation_test"),
                row_factory=dict_row,
                autocommit=True,
            )
            connection.execute(f'SET search_path TO "{db.schema}", pg_catalog')
        else:
            connection = MySQLJobTransactions(
                db.url, db.schema, role, "synthetic_foundation_test"
            ).connect()
            connection.autocommit(True)
        try:
            yield connection
        finally:
            connection.close()


def seed(database: JobDatabase) -> None:
    """Synthetic relational rows only; not evidence of complete recipe admission."""
    database.query(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES ('tts-realtime', 'realtime', 'active')"
    )
    database.query(
        "INSERT INTO workspace_quotas (workspace_id, max_attempts, held_attempts, policy_revision) "
        "VALUES ('workspace-test', 2, 1, 'policy-test')"
    )
    database.query(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, topology_digest, "
        "allocatable_unified_physical_memory_bytes, held_unified_physical_memory_bytes) "
        "VALUES ('host-test', 'unified_memory', 'active', %s, 10, 1)",
        (DIGEST,),
    )
    database.query(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id, "
        "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
        "loading_policy, config_digest, instance_state, max_slots, held_slots) VALUES "
        "('instance-test', 1, 'host-test', 'tts-realtime', 'gateway-test', 'binding-test', "
        "'manifest-test', 'template-test', 'managed', 'on_demand', %s, 'ready', 2, 1)",
        (DIGEST,),
    )
    database.query(
        "INSERT INTO runtime_instance_heads "
        "(instance_id, current_generation, generation_high_water) "
        "VALUES ('instance-test', 1, 1)"
    )
    database.query(
        "INSERT INTO runtime_instance_intents (target_node_id, intent_id, instance_id, "
        "instance_generation, action, request_digest, request_document, intent_state) VALUES "
        "('host-test', 'runtime-test', 'instance-test', 1, 'prepare', %s, %s, 'pending')",
        (DIGEST, '{"synthetic_runtime_body":true}'),
    )
    database.query(
        "INSERT INTO admission_attempts "
        "(operation_id, attempt_id, workspace_id, binding_revision_id, "
        "manifest_id, recipe_digest, policy_revision_id, service_class, request_digest, "
        "required_stage_ids) VALUES ('operation-test', 'attempt-test', 'workspace-test', "
        "'binding-test', 'manifest-test', %s, 'policy-test', 'realtime', %s, %s)",
        (DIGEST, DIGEST, '["stage-main"]'),
    )
    database.query(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, host_node_id, "
        "instance_id, instance_generation, pool_id, gateway_id, fencing_token, reserved_slots, "
        "deadline_at_us, reserved_unified_physical_memory_bytes) VALUES ('lease-test-stage-main', "
        "'operation-test', 'attempt-test', 'stage-main', 'host-test', 'instance-test', 1, "
        "'tts-realtime', 'gateway-test', 1, 1, 1, 1)"
    )
    database.query(
        "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id, "
        "command_digest, request_digest, request_document, deadline_at_us, recorded_at_us, "
        "updated_at_us, output_stream_id, sample_rate_hz, hard_output_ceiling_samples) VALUES "
        "('submit-test', 'lease-test-stage-main', 'operation-test', 'attempt-test', %s, %s, %s, "
        "1, 1, 1, 'tts-main', 24000, 48000)",
        (DIGEST, DIGEST, '{"synthetic_execution_body":true}'),
    )
    database.query(
        "INSERT INTO output_stream_heads "
        "(operation_id, attempt_id, output_stream_id, sample_rate_hz) "
        "VALUES ('operation-test', 'attempt-test', 'tts-main', 24000)"
    )


@pytest.fixture
def foundation(database: JobDatabase) -> Iterator[Foundation]:
    db = database
    roles = (db.schema + "_speech", db.schema + "_gateway")
    created_roles: list[str] = []
    if db.dialect == "mysql":
        for ddl in COORDINATION_DDL:
            ddl = ddl.replace('COLLATE "C"', "COLLATE utf8mb4_0900_bin")
            db.query(re.sub(r"\bTEXT\b", "LONGTEXT", ddl))
        for ddl in (
            *RUNTIME_INTENT_DDL,
            *RUNTIME_OBSERVATION_DDL,
            *ADMISSION_COMMAND_DDL,
            *RUNTIME_DELIVERY_DDL,
            *EXECUTION_RESULT_DDL,
        ):
            ddl = ddl.replace('COLLATE "C"', "COLLATE utf8mb4_0900_bin")
            ddl = ddl.replace(
                "ALTER COLUMN job_id DROP NOT NULL",
                "MODIFY COLUMN job_id VARCHAR(128) COLLATE utf8mb4_0900_bin NULL",
            )
            db.query(re.sub(r"\bTEXT\b", "LONGTEXT", ddl))
    try:
        for role in roles:
            if db.dialect == "postgresql":
                db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_foundation_test'")
            else:
                db.query(f"CREATE USER '{role}'@'%' IDENTIFIED BY 'synthetic_foundation_test'")
            created_roles.append(role)
        for grant in coordination_runtime_grants(
            db.schema, speech_role=roles[0], gateway_role=roles[1]
        ):
            if db.dialect == "mysql":
                if "USAGE ON SCHEMA" in grant:
                    continue
                grant = re.sub(r'TO "([^"]+)"', r"TO '\1'@'%'", grant).replace('"', "`")
            db.query(grant)
        seed(db)
        yield Foundation(db, *roles)
    finally:
        for role in reversed(created_roles):
            if db.dialect == "postgresql":
                db.query(f'DROP OWNED BY "{role}"')
                db.query(f'DROP ROLE "{role}"')
            else:
                db.query(f"DROP USER IF EXISTS '{role}'@'%'")


def test_complete_foundation_has_exact_owned_tables_and_binary_identifiers(
    foundation: Foundation,
) -> None:
    db = foundation.database
    rows = db.query(
        "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = %s",
        (db.schema,),
    )
    asset_tables = (
        {
            "speech_workspaces",
            "speech_assets",
            "speech_asset_commands",
            "speech_asset_objects",
            "speech_asset_references",
            "speech_content_deletions",
            "speech_asset_events",
            "speech_asset_storage_authority",
        }
        if db.dialect == "postgresql"
        else set()
    )
    profile_tables = (
        {
            "speech_profile_families",
            "speech_profile_revisions",
            "speech_profile_commands",
            "speech_profile_lifecycle_intents",
            "speech_pronunciation_families",
            "speech_pronunciation_revisions",
            "speech_pronunciation_commands",
        }
        if db.dialect == "postgresql"
        else set()
    )
    binding_tables = (
        {
            "speech_binding_families",
            "speech_binding_revisions",
            "speech_binding_commands",
            "speech_binding_defaults",
        }
        if db.dialect == "postgresql"
        else set()
    )
    publication_tables = (
        {
            "speech_publications",
            "speech_jobs",
            "speech_job_events",
            "speech_job_submissions",
            # security-barrier view from 0019: the only published binding
            # surface a read-only catalogue credential may reach.
            "speech_published_binding_revisions",
            # 0021: publish-time evidence, one row per publication.
            "speech_binding_publication_evidence",
            # 0022: one transactional catalogue version and visibility tombstones.
            "speech_catalog_state",
            "speech_publication_retirements",
            # 0023: explicit publication selection and append-only lifecycle.
            "speech_publication_heads",
            "speech_publication_lifecycle_events",
        }
        if db.dialect == "postgresql"
        else set()
    )
    # Artifact metadata (0020). Reads go through their own connection; the
    # table stays outside the shared coordination surface.
    artifact_tables = {"speech_artifacts"} if db.dialect == "postgresql" else set()
    assert binding_tables <= {row["name"] for row in rows}
    assert profile_tables <= {row["name"] for row in rows}
    assert publication_tables <= {row["name"] for row in rows}
    assert artifact_tables <= {row["name"] for row in rows}
    assert {row["name"] for row in rows} - {
        "alembic_version"
    } - asset_tables - profile_tables - binding_tables - publication_tables - artifact_tables == (
        set(SPEECH_UPDATES) | set(GATEWAY_UPDATES)
    )
    if db.dialect == "postgresql":
        # New configuration and publication tables are outside shared coordination grants.
        for owner in ("speech", "gateway"):
            for table in profile_tables | binding_tables | publication_tables:
                with (
                    pytest.raises(psycopg.errors.InsufficientPrivilege),
                    foundation.connection(owner) as connection,
                    connection.cursor() as cursor,
                ):
                    cursor.execute(f"SELECT * FROM {table}")
    db.query(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES ('TTS-realtime', 'realtime', 'active')"
    )
    assert db.query("SELECT count(*) AS n FROM resource_pools")[0]["n"] == 2


def test_basic_budgets_foreign_keys_and_frozen_output_constraints(foundation: Foundation) -> None:
    db = foundation.database
    for statement, pgcode, mysqlcode in (
        ("UPDATE engine_instances SET host_node_id = 'missing'", "23503", 1452),
        ("UPDATE admission_leases SET gateway_id = 'other'", "23503", 1452),
        (
            "UPDATE runtime_instance_heads SET current_generation = 2, generation_high_water = 2",
            "23503",
            1452,
        ),
        ("UPDATE workspace_quotas SET held_attempts = 3", "23514", 3819),
        ("UPDATE host_nodes SET held_unified_physical_memory_bytes = 11", "23514", 3819),
        ("UPDATE engine_instances SET held_slots = 3", "23514", 3819),
        ("UPDATE submit_intents SET request_document = NULL", "23514", 3819),
        ("UPDATE submit_intents SET sample_rate_hz = NULL", "23514", 3819),
        ("UPDATE submit_intents SET generated_pcm_samples = 48001", "23514", 3819),
        ("UPDATE submit_intents SET recorded_at_us = -1", "23514", 3819),
    ):
        with pytest.raises((psycopg.IntegrityError, pymysql.MySQLError)) as failure:
            db.query(statement)
        if db.dialect == "postgresql":
            assert getattr(failure.value, "sqlstate", None) == pgcode
        else:
            assert failure.value.args[0] in ({1451, 1452} if pgcode == "23503" else {mysqlcode})


def test_minimum_roles_allow_own_projection_and_foreign_row_locks(foundation: Foundation) -> None:
    for owner, own, foreign in (
        ("speech", "admission_leases", "submit_intents"),
        ("gateway", "submit_intents", "admission_leases"),
    ):
        with foundation.connection(owner) as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE {own} SET version = version + 1")
            assert cursor.rowcount == 1
            cursor.execute("BEGIN")
            try:
                cursor.execute(f"SELECT version FROM {foreign} FOR UPDATE")
                assert cursor.fetchone()["version"] >= 1
            finally:
                connection.rollback()


def test_roles_cannot_read_foreign_content_rewrite_identity_raise_capacity_or_erase_history(
    foundation: Foundation,
) -> None:
    for owner, statement in (
        ("speech", "SELECT request_document FROM submit_intents"),
        ("gateway", "SELECT request_document FROM runtime_instance_intents"),
        ("speech", "UPDATE submit_intents SET execution_state = 'succeeded'"),
        ("gateway", "UPDATE admission_leases SET lease_state = 'released'"),
        ("gateway", "UPDATE submit_intents SET command_digest = 'forged'"),
        ("speech", "UPDATE admission_attempts SET request_digest = 'forged'"),
        ("speech", "UPDATE workspace_quotas SET max_attempts = 100"),
        ("speech", "UPDATE workspace_quotas SET policy_revision = 'other'"),
        ("speech", "UPDATE host_nodes SET allocatable_device_memory_bytes = 999"),
        ("speech", "UPDATE host_nodes SET topology_digest = 'forged'"),
        ("speech", "UPDATE host_nodes SET quarantine_threshold_ppm = 0"),
        (
            "speech",
            "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
            "VALUES ('unauthorized', 'batch', 'active')",
        ),
        (
            "speech",
            "INSERT INTO workspace_quotas (workspace_id, max_attempts, policy_revision) "
            "VALUES ('unauthorized', 100, 'forged')",
        ),
        ("gateway", "UPDATE execution_stop_evidence SET evidence_digest = 'forged'"),
        ("gateway", "UPDATE output_sample_intervals SET sample_end = 999"),
        ("gateway", "DELETE FROM submit_intents"),
        ("speech", "DELETE FROM admission_leases"),
        ("gateway", "TRUNCATE output_sample_intervals"),
        ("speech", "CREATE TABLE unauthorized_table (id BIGINT)"),
        ("gateway", "SELECT version_num FROM alembic_version"),
    ):
        if foundation.database.dialect == "mysql" and "alembic_version" in statement:
            continue  # Product migration metadata exists only on PostgreSQL.
        with foundation.connection(owner) as connection, connection.cursor() as cursor:
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, pymysql.OperationalError)
            ) as failure:
                cursor.execute(statement)
            if foundation.database.dialect == "mysql":
                assert failure.value.args[0] in {1142, 1143}


def test_only_gateway_can_append_stop_and_interval_facts(foundation: Foundation) -> None:
    statements = (
        (
            "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, evidence_kind, "
            "evidence_digest, observed_at_us, execution_stopped, resources_released) "
            "VALUES ('evidence-test', 'submit-test', 'engine_terminal_response', %s, 1, 1, 1)",
            (DIGEST,),
        ),
        (
            "INSERT INTO output_sample_intervals (interval_id, submit_intent_id, operation_id, "
            "attempt_id, output_stream_id, meter_kind, sample_start, sample_end) "
            "VALUES ('interval-test', 'submit-test', 'operation-test', 'attempt-test', "
            "'tts-main', 'generated', 0, 24000)",
            (),
        ),
    )
    for statement, values in statements:
        with (
            foundation.connection("speech") as connection,
            connection.cursor() as cursor,
            pytest.raises((psycopg.errors.InsufficientPrivilege, pymysql.OperationalError)),
        ):
            cursor.execute(statement, values or None)
        with foundation.connection("gateway") as connection, connection.cursor() as cursor:
            cursor.execute(statement, values or None)
            assert cursor.rowcount == 1


def test_command_redaction_requires_a_complete_tombstone(foundation: Foundation) -> None:
    # Fixture preparation only. Application release/cleanup ordering is not
    # implemented or qualified by this storage/privilege test.
    foundation.database.query(
        "UPDATE submit_intents SET execution_state = 'aborted', cleanup_state = 'complete'"
    )
    foundation.database.query(
        "UPDATE admission_leases SET lease_state = 'released', cleanup_state = 'complete', "
        "budget_released_at_us = 2"
    )
    with foundation.connection("gateway") as connection, connection.cursor() as cursor:
        with pytest.raises((psycopg.IntegrityError, pymysql.MySQLError)) as failure:
            cursor.execute("UPDATE submit_intents SET request_document = NULL")
        if foundation.database.dialect == "mysql":
            assert failure.value.args[0] == 3819
        cursor.execute(
            "UPDATE submit_intents SET request_document = NULL, command_redacted_at_us = 2, "
            "command_deletion_job_id = 'deletion-test', command_deletion_evidence_digest = %s",
            (DIGEST,),
        )
        assert cursor.rowcount == 1
    row = foundation.database.query("SELECT * FROM submit_intents")[0]
    assert row["request_document"] is None
    assert row["command_digest"] == DIGEST
    assert row["lease_id"] == "lease-test-stage-main"


def test_canonical_documents_over_64k_are_retained_and_private(foundation: Foundation) -> None:
    payload = json.dumps({"synthetic": "x" * 70000})
    foundation.database.query("UPDATE submit_intents SET request_document = %s", (payload,))
    with foundation.connection("gateway") as connection, connection.cursor() as cursor:
        cursor.execute("SELECT request_document FROM submit_intents")
        assert cursor.fetchone()["request_document"] == payload


@dataclass
class UpgradeSandbox:
    schema: str
    engine: Engine = field(repr=False)
    config: Config = field(repr=False)
    admin: Any = field(repr=False)

    def upgrade(self, revision: str = "head") -> None:
        with self.engine.connect() as connection:
            self.config.attributes["connection"] = connection
            command.upgrade(self.config, revision)

    def version(self) -> str:
        return cast(
            str,
            self.admin.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"],
        )


@pytest.fixture
def upgrade_sandbox(database: JobDatabase) -> Iterator[UpgradeSandbox]:
    assert database.dialect == "postgresql"
    schema = "sn_upgrade_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        database.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    admin = psycopg.connect(database.url, row_factory=dict_row, autocommit=True)
    sandbox = UpgradeSandbox(schema, engine, config, admin)
    try:
        sandbox.upgrade("0001_jobs")
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        for grant in job_runtime_grants(schema, database.role):
            admin.execute(grant)
        with admin.transaction(), admin.cursor() as cursor:
            assert create_in_transaction(cast(JobCursor, cursor), REQUEST).outcome == "committed"
        yield sandbox
    finally:
        admin.close()
        engine.dispose()
        database.query(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_failed_upgrade_rolls_back_all_new_ddl_and_preserves_old_history(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.admin.execute("CREATE TABLE output_sample_intervals (sentinel BIGINT)")
    with pytest.raises(ProgrammingError) as failure:
        sandbox.upgrade()
    assert getattr(failure.value.orig, "sqlstate", None) == "42P07"
    assert sandbox.version() == "0001_jobs"
    tables = sandbox.admin.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (sandbox.schema,),
    ).fetchall()
    assert {row["table_name"] for row in tables} == {
        "coordination_jobs",
        "coordination_outbox",
        "alembic_version",
        "output_sample_intervals",
    }
    sandbox.admin.execute("DROP TABLE output_sample_intervals")
    acl = sandbox.admin.execute(
        "SELECT relname, relacl FROM pg_class WHERE relnamespace = %s::regnamespace "
        "AND relname IN ('coordination_jobs', 'coordination_outbox') ORDER BY relname",
        (sandbox.schema,),
    ).fetchall()
    sandbox.upgrade()
    sandbox.upgrade()
    assert sandbox.version() == HEAD
    assert sandbox.admin.execute("SELECT count(*) AS n FROM coordination_jobs").fetchone()["n"] == 1
    assert (
        sandbox.admin.execute("SELECT count(*) AS n FROM coordination_outbox").fetchone()["n"] == 1
    )
    assert (
        sandbox.admin.execute(
            "SELECT relname, relacl FROM pg_class WHERE relnamespace = %s::regnamespace "
            "AND relname IN ('coordination_jobs', 'coordination_outbox') ORDER BY relname",
            (sandbox.schema,),
        ).fetchall()
        == acl
    )


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_separate_migration_processes_wait_on_the_same_transaction_lock(
    database: JobDatabase,
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    blocker = psycopg.connect(database.url)
    applications = [sandbox.schema + "_first", sandbox.schema + "_second"]
    processes: list[subprocess.Popen[str]] = []
    try:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("speechnexus:migrations:" + sandbox.schema,),
        )
        for application in applications:
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "speechnexus_backend.speech.infrastructure.migrate_jobs",
                        "--schema",
                        sandbox.schema,
                    ],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "SPEECHNEXUS_MIGRATION_DATABASE_URL": database.url,
                        "PGAPPNAME": application,
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            waiting = sandbox.admin.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = ANY(%s) "
                "AND cardinality(pg_blocking_pids(pid)) > 0",
                (applications,),
            ).fetchone()["n"]
            if waiting == 2:
                break
            time.sleep(0.02)
        else:
            pytest.fail("Both independent migration processes must show real database lock waits")
        assert sandbox.version() == "0001_jobs"
        blocker.commit()
        for process in processes:
            output, error = process.communicate(timeout=15)
            assert process.returncode == 0, output + error
        assert sandbox.version() == HEAD
        assert (
            sandbox.admin.execute("SELECT count(*) AS n FROM coordination_jobs").fetchone()["n"]
            == 1
        )
    finally:
        blocker.rollback()
        blocker.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
