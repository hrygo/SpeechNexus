"""Isolated PG: consistent abort recovery and the independent reader's real ACLs."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command as migrations
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from speechnexus_contracts.execution import parse
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.execution_command import command_example
from tests.integration.test_coordination_foundation import seed
from tests.integration.test_gateway_submit_ledger import identity_for
from tests.integration.test_production_jobs import JobDatabase
from tests.support.execution_reader_database import isolated_reader_database

from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    gateway_query_grants,
    gateway_query_target_grants,
)
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.submission import SubmissionStoreError, SubmitRequest
from speechnexus_gateway.infrastructure import execution_reader
from speechnexus_gateway.infrastructure.execution_reader import (
    PostgresExecutionReader,
    verify_execution_reader_authority,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool, gateway_pool
from speechnexus_gateway.infrastructure.reader_authority import verify_execution_reader_target
from speechnexus_gateway.infrastructure.submit_sql import read_submit_intent, to_unix_micros

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "synthetic_reader_test"
_LO_FUNCTIONS = (
    "lo_create",
    "lo_creat",
    "lo_from_bytea",
    "lo_import",
    "lo_export",
    "lo_unlink",
    "lo_put",
    "lo_truncate",
    "lo_truncate64",
    "lowrite",
)


@dataclass
class ReaderDatabase:
    url: str
    schema: str
    admin: Any
    request: SubmitRequest

    def sql(self, query: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        cursor = self.admin.execute(query, params)
        return list(cursor.fetchall()) if cursor.description else []


@pytest.fixture(scope="module")
def reader_database() -> Iterator[ReaderDatabase]:
    root_url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not root_url:
        pytest.skip("An explicitly configured isolated PostgreSQL server is required")
    database_name = "sn_abort_reader_" + uuid.uuid4().hex[:12]
    schema = "sn_reader_" + uuid.uuid4().hex[:12]
    root = psycopg.connect(root_url, autocommit=True)
    created = False
    admin = None
    try:
        root.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database_name))
        )
        created = True
        url = make_conninfo(root_url, dbname=database_name)
        # Alembic accepts a SQLAlchemy URL, while psycopg's make_conninfo returns
        # keyword syntax; let psycopg parse it via the creator instead of logging it.
        engine = create_engine(
            "postgresql+psycopg://",
            creator=lambda: psycopg.connect(url),
            poolclass=NullPool,
            hide_parameters=True,
        )
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["schema"] = schema
        try:
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                migrations.upgrade(config, "head")
        finally:
            engine.dispose()
        admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        admin.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
        admin.execute(
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(database_name)
            )
        )
        signatures = admin.execute(
            "SELECT p.oid::pg_catalog.regprocedure::text AS signature FROM pg_catalog.pg_proc p "
            "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='pg_catalog' AND p.proname=ANY(%s)",
            (list(_LO_FUNCTIONS),),
        ).fetchall()
        assert signatures
        for row in signatures:
            # Signatures come exclusively from pg_catalog in this disposable database.
            admin.execute(
                sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC").format(
                    sql.SQL(row["signature"])
                )
            )
        db = JobDatabase("postgresql", url, schema, "", admin, cast(Any, None), None)
        seed(db)
        command = command_example(0)
        identity = replace(
            identity_for(
                "lease-test-stage-main", submit_intent_id="submit-test", attempt_id="attempt-test"
            ),
            command_digest=command_digest(command),
        )
        request = SubmitRequest(identity, command, "tts-main")
        now_us = to_unix_micros(datetime.now(UTC))
        admin.execute(
            "UPDATE submit_intents SET command_digest=%s,request_digest=%s,request_document=%s,"
            "deadline_at_us=%s,recorded_at_us=%s,updated_at_us=%s "
            "WHERE submit_intent_id='submit-test'",
            (
                identity.command_digest,
                request.request_digest,
                request.request_document,
                to_unix_micros(identity.deadline_at),
                now_us,
                now_us,
            ),
        )
        yield ReaderDatabase(url, schema, admin, request)
    finally:
        if admin is not None:
            admin.close()
        if created:
            root.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))
        root.close()


@pytest.fixture
def reader(reader_database: ReaderDatabase) -> Iterator[tuple[str, GatewayPool]]:
    db = reader_database
    role = "sn_reader_role_" + uuid.uuid4().hex[:12]
    db.admin.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(PASSWORD)
        )
    )
    pool: GatewayPool | None = None
    try:
        for statement in gateway_query_grants(db.schema, role):
            db.admin.execute(statement)
        pool = gateway_pool(make_conninfo(db.url, user=role, password=PASSWORD), schema=db.schema)
        pool.open()
        yield role, pool
    finally:
        if pool is not None:
            pool.close()
        db.admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        db.admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture
def writer_target(reader_database: ReaderDatabase) -> Iterator[GatewayPool]:
    db = reader_database
    role = "sn_writer_target_" + uuid.uuid4().hex[:12]
    db.admin.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(PASSWORD)
        )
    )
    pool: GatewayPool | None = None
    try:
        for statement in gateway_query_target_grants(db.schema, role):
            db.admin.execute(statement)
        pool = gateway_pool(make_conninfo(db.url, user=role, password=PASSWORD), schema=db.schema)
        pool.open()
        yield pool
    finally:
        if pool is not None:
            pool.close()
        db.admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        db.admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _insert_receipt(db: ReaderDatabase) -> None:
    db.sql(
        "INSERT INTO gateway_abort_intents "
        "(submit_intent_id,operation_id,attempt_id,fencing_token,reason,wait_timeout_ms,"
        "received_at_us) "
        "VALUES ('submit-test','operation-test','attempt-test',1,'client_cancelled',1000,%s)",
        (to_unix_micros(datetime.now(UTC)),),
    )


def test_reader_has_only_required_access_and_recovers_receipt(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    _, pool = reader
    with pool.connection() as connection:
        assert connection.execute(
            "SELECT pg_catalog.has_table_privilege(current_user,'pg_catalog.pg_settings',"
            "'UPDATE') AS allowed"
        ).fetchone() == {"allowed": True}
    assert verify_execution_reader_authority(pool, db.schema)
    query = PostgresExecutionReader(pool)
    original = query.query_outcome("submit-test")
    assert original is not None
    assert cast(dict[str, Any], original.status()["abort_recovery"])["document_state"] == "verified"
    _insert_receipt(db)
    recovered = query.query_outcome("submit-test")
    assert recovered is not None
    receipt = cast(dict[str, Any], recovered.status()["abort_recovery"])["receipt"]
    assert receipt is not None and receipt["claim_state"] == "received"
    parse("ExecutionRecoveryStatusResponse", recovered.status())
    db.sql(
        "UPDATE gateway_abort_intents SET dispatched_at_us=received_at_us+1 "
        "WHERE submit_intent_id='submit-test'"
    )
    claimed = query.query_outcome("submit-test")
    assert claimed is not None
    claimed_receipt = cast(dict[str, Any], claimed.status()["abort_recovery"])["receipt"]
    assert claimed_receipt["claim_state"] == "claimed" and claimed_receipt["claimed_at"] is not None
    assert claimed.status()["execution_state"] == original.status()["execution_state"]
    parse("ExecutionRecoveryStatusResponse", claimed.status())
    with pytest.raises(psycopg.errors.InsufficientPrivilege), pool.connection() as connection:
        connection.execute("UPDATE submit_intents SET execution_state='failed'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), pool.connection() as connection:
        connection.execute("SELECT request_document FROM runtime_instance_intents")
    assert "client_cancelled" not in repr(recovered.status())
    db.sql("DELETE FROM gateway_abort_intents WHERE submit_intent_id='submit-test'")


def test_target_is_pinned_and_rechecked_without_a_live_writer(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], writer_target: GatewayPool
) -> None:
    db = reader_database
    _, pool = reader
    target = verify_execution_reader_target(writer_target, pool, db.schema)
    assert target is not None
    query = PostgresExecutionReader(pool, expected_target=target)
    writer_target.close()
    assert query.query_outcome("submit-test") is not None
    original = db.sql("SELECT installation_id FROM speech_publication_installation")[0]
    db.sql("UPDATE speech_publication_installation SET installation_id=gen_random_uuid()")
    try:
        with pytest.raises(SubmissionStoreError):
            query.query_outcome("submit-test")
    finally:
        db.sql(
            "UPDATE speech_publication_installation SET installation_id=%s",
            (original["installation_id"],),
        )
    assert query.query_outcome("submit-test") is not None


def test_same_schema_and_copied_installation_in_another_database_is_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], writer_target: GatewayPool
) -> None:
    db = reader_database
    role, _ = reader
    root_url = os.environ["SPEECHNEXUS_TEST_DATABASE_URL"]
    installation = db.sql("SELECT installation_id FROM speech_publication_installation")[0]
    with isolated_reader_database(root_url) as other_url:
        with psycopg.connect(other_url, autocommit=True) as other:
            other.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(db.schema)))
            other.execute(
                sql.SQL(
                    "CREATE TABLE {}.speech_publication_installation "
                    "(singleton boolean PRIMARY KEY,installation_id uuid NOT NULL)"
                ).format(sql.Identifier(db.schema))
            )
            other.execute(
                sql.SQL("INSERT INTO {}.speech_publication_installation VALUES (true,%s)").format(
                    sql.Identifier(db.schema)
                ),
                (installation["installation_id"],),
            )
            for statement in gateway_query_target_grants(db.schema, role):
                other.execute(statement)
        other_pool = gateway_pool(
            make_conninfo(other_url, user=role, password=PASSWORD), schema=db.schema
        )
        other_pool.open()
        try:
            assert verify_execution_reader_target(writer_target, other_pool, db.schema) is None
        finally:
            other_pool.close()


def test_receipt_committed_mid_query_is_not_mixed_into_old_snapshot(
    reader_database: ReaderDatabase,
    reader: tuple[str, GatewayPool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = reader_database
    _, pool = reader
    original_read = read_submit_intent
    inserted = False

    def interleaved(*args: Any) -> Any:
        nonlocal inserted
        result = original_read(*args)
        if not inserted:
            _insert_receipt(db)
            inserted = True
        return result

    monkeypatch.setattr(execution_reader, "read_submit_intent", interleaved)
    query = PostgresExecutionReader(pool)
    before = query.query_outcome("submit-test")
    after = query.query_outcome("submit-test")
    assert before is not None and after is not None
    assert cast(dict[str, Any], before.status()["abort_recovery"])["receipt"] is None
    assert cast(dict[str, Any], after.status()["abort_recovery"])["receipt"] is not None
    db.sql("DELETE FROM gateway_abort_intents WHERE submit_intent_id='submit-test'")


@pytest.mark.parametrize("membership", ["INHERIT TRUE, SET FALSE", "INHERIT FALSE, SET TRUE"])
def test_write_via_inheritance_or_set_role_is_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], membership: str
) -> None:
    db = reader_database
    role, pool = reader
    group = "sn_reader_group_" + uuid.uuid4().hex[:12]
    db.admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(group)))
    try:
        db.admin.execute(
            sql.SQL("GRANT UPDATE (execution_state) ON {}.submit_intents TO {}").format(
                sql.Identifier(db.schema), sql.Identifier(group)
            )
        )
        db.admin.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(db.schema), sql.Identifier(group)
            )
        )
        db.admin.execute(
            sql.SQL("GRANT {} TO {} WITH " + membership + ", ADMIN FALSE").format(
                sql.Identifier(group), sql.Identifier(role)
            )
        )
        with pool.connection() as connection:
            if "SET TRUE" in membership:
                connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(group)))
            connection.execute("UPDATE submit_intents SET execution_state='submitting'")
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(group), sql.Identifier(role))
        )
        db.admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(group)))
        db.admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(group)))


def test_public_temp_and_write_are_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    _, pool = reader
    name = db.admin.info.dbname
    assert name is not None
    db.admin.execute(
        sql.SQL("GRANT TEMPORARY ON DATABASE {} TO PUBLIC").format(sql.Identifier(name))
    )
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
        )
    db.admin.execute(
        sql.SQL("GRANT UPDATE (execution_state) ON {}.submit_intents TO PUBLIC").format(
            sql.Identifier(db.schema)
        )
    )
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL("REVOKE UPDATE (execution_state) ON {}.submit_intents FROM PUBLIC").format(
                sql.Identifier(db.schema)
            )
        )
    db.admin.execute(
        sql.SQL("GRANT SELECT (request_document) ON {}.runtime_instance_intents TO PUBLIC").format(
            sql.Identifier(db.schema)
        )
    )
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL(
                "REVOKE SELECT (request_document) ON {}.runtime_instance_intents FROM PUBLIC"
            ).format(sql.Identifier(db.schema))
        )


@pytest.mark.parametrize("capability", ["pg_write_all_data", "pg_read_server_files"])
def test_predefined_role_even_without_set_is_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], capability: str
) -> None:
    db = reader_database
    role, pool = reader
    db.admin.execute(
        sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE").format(
            sql.Identifier(capability), sql.Identifier(role)
        )
    )
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(capability), sql.Identifier(role))
        )


def test_sequence_and_security_definer_bypasses_are_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    role, pool = reader
    db.sql("CREATE SEQUENCE reader_test_sequence")
    try:
        db.admin.execute(
            sql.SQL("GRANT USAGE ON SEQUENCE {}.reader_test_sequence TO {}").format(
                sql.Identifier(db.schema), sql.Identifier(role)
            )
        )
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT nextval('reader_test_sequence') AS value"
            ).fetchone() == {"value": 1}
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.sql("DROP SEQUENCE reader_test_sequence")
    db.sql("CREATE TABLE reader_definer_marker (value integer)")
    db.sql(
        "CREATE FUNCTION reader_test_definer() RETURNS integer LANGUAGE SQL SECURITY DEFINER "
        "AS 'INSERT INTO reader_definer_marker VALUES (1) RETURNING value'"
    )
    try:
        with pool.connection() as connection:
            assert connection.execute("SELECT reader_test_definer() AS value").fetchone() == {
                "value": 1
            }
        assert db.sql("SELECT value FROM reader_definer_marker") == [{"value": 1}]
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.sql("DROP FUNCTION reader_test_definer()")
        db.sql("DROP TABLE reader_definer_marker")


def test_security_definer_outside_application_schema_is_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    _, pool = reader
    db.sql(
        "CREATE FUNCTION information_schema.reader_test_definer() RETURNS integer "
        "LANGUAGE SQL SECURITY DEFINER AS 'SELECT 1'"
    )
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.sql("DROP FUNCTION information_schema.reader_test_definer()")


def test_column_grant_option_cannot_delegate_private_command_access(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    role, pool = reader
    recipient = "sn_reader_extra_" + uuid.uuid4().hex[:12]
    db.admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(recipient)))
    db.admin.execute(
        sql.SQL(
            "GRANT SELECT (request_document) ON {}.submit_intents TO {} WITH GRANT OPTION"
        ).format(sql.Identifier(db.schema), sql.Identifier(role))
    )
    try:
        with pool.connection() as connection:
            connection.execute(
                sql.SQL("GRANT SELECT (request_document) ON {}.submit_intents TO {}").format(
                    sql.Identifier(db.schema), sql.Identifier(recipient)
                )
            )
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL(
                "REVOKE GRANT OPTION FOR SELECT (request_document) ON {}.submit_intents "
                "FROM {} CASCADE"
            ).format(sql.Identifier(db.schema), sql.Identifier(role))
        )
        db.admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(recipient)))
        db.admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(recipient)))


@pytest.mark.parametrize("authority", ["schema_create", "table_update", "column_update"])
def test_catalog_create_and_write_privileges_are_never_trusted(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], authority: str
) -> None:
    db = reader_database
    role, pool = reader
    permission = {
        "schema_create": "CREATE ON SCHEMA pg_catalog",
        "table_update": "UPDATE ON TABLE pg_catalog.pg_class",
        "column_update": "UPDATE (relpages) ON TABLE pg_catalog.pg_class",
    }[authority]
    db.admin.execute(sql.SQL("GRANT " + permission + " TO {}").format(sql.Identifier(role)))
    try:
        with pool.connection() as connection:
            if authority == "schema_create":
                fact = connection.execute(
                    "SELECT pg_catalog.has_schema_privilege("
                    "current_user,'pg_catalog','CREATE') AS allowed"
                ).fetchone()
            elif authority == "table_update":
                # PostgreSQL may mask system-table writes in has_table_privilege
                # even though the explicit ACL itself grants UPDATE.
                fact = db.admin.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a "
                    "JOIN pg_catalog.pg_roles r ON r.oid=a.grantee "
                    "WHERE c.oid='pg_catalog.pg_class'::pg_catalog.regclass "
                    "AND r.rolname=%s AND a.privilege_type='UPDATE') AS allowed",
                    (role,),
                ).fetchone()
            else:
                fact = connection.execute(
                    "SELECT pg_catalog.has_column_privilege("
                    "current_user,'pg_catalog.pg_class','relpages','UPDATE') AS allowed"
                ).fetchone()
            assert fact == {"allowed": True}
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(sql.SQL("REVOKE " + permission + " FROM {}").format(sql.Identifier(role)))


def test_catalog_sensitive_read_and_public_settings_write_are_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    role, pool = reader
    db.admin.execute(
        sql.SQL("GRANT SELECT ON pg_catalog.pg_authid TO {}").format(sql.Identifier(role))
    )
    try:
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.has_table_privilege("
                "current_user,'pg_catalog.pg_authid','SELECT') AS allowed"
            ).fetchone() == {"allowed": True}
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL("REVOKE SELECT ON pg_catalog.pg_authid FROM {}").format(sql.Identifier(role))
        )
    db.sql("GRANT SELECT ON pg_catalog.pg_authid TO PUBLIC")
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.sql("REVOKE SELECT ON pg_catalog.pg_authid FROM PUBLIC")
    db.sql("GRANT DELETE ON pg_catalog.pg_settings TO PUBLIC")
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.sql("REVOKE DELETE ON pg_catalog.pg_settings FROM PUBLIC")
    db.admin.execute(
        sql.SQL("GRANT UPDATE ON pg_catalog.pg_settings TO {} WITH GRANT OPTION").format(
            sql.Identifier(role)
        )
    )
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL("REVOKE UPDATE ON pg_catalog.pg_settings FROM {}").format(sql.Identifier(role))
        )
    db.admin.execute(
        sql.SQL("GRANT UPDATE (setting) ON pg_catalog.pg_settings TO {}").format(
            sql.Identifier(role)
        )
    )
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL("REVOKE UPDATE (setting) ON pg_catalog.pg_settings FROM {}").format(
                sql.Identifier(role)
            )
        )


def test_alter_system_parameter_privilege_is_rejected_without_changing_settings(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    role, pool = reader
    # Parameter ACLs are cluster-wide: grant only to this test's unique role.
    target = sql.Identifier(role)
    db.admin.execute(
        sql.SQL("GRANT ALTER SYSTEM ON PARAMETER log_min_duration_statement TO {}").format(target)
    )
    try:
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.has_parameter_privilege("
                "current_user,'log_min_duration_statement','ALTER SYSTEM') AS allowed"
            ).fetchone() == {"allowed": True}
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(
            sql.SQL("REVOKE ALTER SYSTEM ON PARAMETER log_min_duration_statement FROM {}").format(
                target
            )
        )


def test_catalog_security_definer_and_sequence_are_audited(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    role, pool = reader
    db.sql("SET allow_system_table_mods=on")
    try:
        db.sql(
            "CREATE FUNCTION pg_catalog.sn_reader_test_definer() RETURNS integer "
            "LANGUAGE SQL SECURITY DEFINER AS 'SELECT 1'"
        )
        try:
            assert not verify_execution_reader_authority(pool, db.schema)
        finally:
            db.sql("DROP FUNCTION pg_catalog.sn_reader_test_definer()")
        db.sql("CREATE SEQUENCE pg_catalog.sn_reader_test_sequence")
        try:
            db.admin.execute(
                sql.SQL("GRANT USAGE ON SEQUENCE pg_catalog.sn_reader_test_sequence TO {}").format(
                    sql.Identifier(role)
                )
            )
            assert not verify_execution_reader_authority(pool, db.schema)
        finally:
            db.sql("DROP SEQUENCE pg_catalog.sn_reader_test_sequence")
    finally:
        db.sql("RESET allow_system_table_mods")


@pytest.mark.parametrize("kind", ["enum", "domain"])
def test_owned_type_is_rejected_without_attempting_owner_ddl(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], kind: str
) -> None:
    db = reader_database
    role, pool = reader
    object_name = "sn_reader_type_" + uuid.uuid4().hex[:12]
    object_identifier = sql.Identifier(db.schema, object_name)
    if kind == "enum":
        db.admin.execute(sql.SQL("CREATE TYPE {} AS ENUM ('one')").format(object_identifier))
        command = "ALTER TYPE {} OWNER TO {}"
        cleanup = "DROP TYPE {}"
    else:
        db.admin.execute(sql.SQL("CREATE DOMAIN {} AS text").format(object_identifier))
        command = "ALTER DOMAIN {} OWNER TO {}"
        cleanup = "DROP DOMAIN {}"
    try:
        db.admin.execute(sql.SQL(command).format(object_identifier, sql.Identifier(role)))
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_type t "
                "JOIN pg_catalog.pg_roles r ON r.oid=t.typowner "
                "WHERE t.typname=%s AND r.rolname=current_user) AS owns",
                (object_name,),
            ).fetchone() == {"owns": True}
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.admin.execute(sql.SQL(cleanup).format(object_identifier))


def test_public_large_object_writer_is_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    _, pool = reader
    with pytest.raises(psycopg.errors.InsufficientPrivilege), pool.connection() as connection:
        connection.execute("SELECT pg_catalog.lo_create(0)")
    db.sql("GRANT EXECUTE ON FUNCTION pg_catalog.lo_create(oid) TO PUBLIC")
    try:
        with pool.connection() as connection:
            created = connection.execute("SELECT pg_catalog.lo_create(0) AS oid").fetchone()
            assert created is not None
        assert db.admin.execute("SELECT pg_catalog.lo_unlink(%s)", (created["oid"],)).fetchone()
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.sql("REVOKE EXECUTE ON FUNCTION pg_catalog.lo_create(oid) FROM PUBLIC")


@pytest.mark.parametrize(
    "authority", ["schema_create", "schema_owner", "relation_owner", "database_owner"]
)
def test_schema_and_database_or_relation_owners_are_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], authority: str
) -> None:
    db = reader_database
    role, pool = reader
    current_owner = db.admin.execute("SELECT CURRENT_USER AS owner").fetchone()["owner"]
    database_name = db.admin.info.dbname
    assert database_name is not None
    target = {
        "schema_create": sql.SQL("SCHEMA {} TO {}").format(
            sql.Identifier(db.schema), sql.Identifier(role)
        ),
        "schema_owner": sql.SQL("SCHEMA {} OWNER TO {}").format(
            sql.Identifier(db.schema), sql.Identifier(role)
        ),
        "relation_owner": sql.SQL("TABLE {}.submit_intents OWNER TO {}").format(
            sql.Identifier(db.schema), sql.Identifier(role)
        ),
        "database_owner": sql.SQL("DATABASE {} OWNER TO {}").format(
            sql.Identifier(database_name), sql.Identifier(role)
        ),
    }[authority]
    if authority == "schema_create":
        db.admin.execute(sql.SQL("GRANT CREATE ON ") + target)
    else:
        db.admin.execute(sql.SQL("ALTER ") + target)
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        if authority == "schema_create":
            db.admin.execute(
                sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
                    sql.Identifier(db.schema), sql.Identifier(role)
                )
            )
        elif authority == "schema_owner":
            db.admin.execute(
                sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                    sql.Identifier(db.schema), sql.Identifier(current_owner)
                )
            )
        elif authority == "relation_owner":
            db.admin.execute(
                sql.SQL("ALTER TABLE {}.submit_intents OWNER TO {}").format(
                    sql.Identifier(db.schema), sql.Identifier(current_owner)
                )
            )
        else:
            db.admin.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(database_name), sql.Identifier(current_owner)
                )
            )


def test_missing_column_and_row_security_fail_closed(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    role, pool = reader
    db.admin.execute(
        sql.SQL("REVOKE SELECT (reason) ON {}.gateway_abort_intents FROM {}").format(
            sql.Identifier(db.schema), sql.Identifier(role)
        )
    )
    assert not verify_execution_reader_authority(pool, db.schema)
    db.admin.execute(
        sql.SQL("GRANT SELECT (reason) ON {}.gateway_abort_intents TO {}").format(
            sql.Identifier(db.schema), sql.Identifier(role)
        )
    )
    db.sql("ALTER TABLE submit_intents ENABLE ROW LEVEL SECURITY")
    try:
        assert not verify_execution_reader_authority(pool, db.schema)
    finally:
        db.sql("ALTER TABLE submit_intents DISABLE ROW LEVEL SECURITY")


def test_deleted_original_and_corrupted_original_have_distinct_safe_outcomes(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    _, pool = reader
    before = db.sql(
        "SELECT request_document,execution_state,cleanup_state,error_code,updated_at_us,version "
        "FROM submit_intents WHERE submit_intent_id='submit-test'"
    )[0]
    lease = db.sql(
        "SELECT lease_state FROM admission_leases WHERE lease_id='lease-test-stage-main'"
    )[0]
    _insert_receipt(db)
    query = PostgresExecutionReader(pool)
    try:
        db.sql(
            "UPDATE submit_intents SET request_document='{}' WHERE submit_intent_id='submit-test'"
        )
        with pytest.raises(SubmissionStoreError):
            query.query_outcome("submit-test")
        db.sql(
            "UPDATE submit_intents SET request_document=%s WHERE submit_intent_id='submit-test'",
            (before["request_document"],),
        )
        db.sql(
            "UPDATE admission_leases SET lease_state='released' "
            "WHERE lease_id='lease-test-stage-main'"
        )
        redacted_at_us = to_unix_micros(datetime.now(UTC))
        db.sql(
            "UPDATE submit_intents SET request_document=NULL,command_redacted_at_us=%s,"
            "command_deletion_job_id='delete-test',command_deletion_evidence_digest=%s,"
            "execution_state='failed',cleanup_state='not_required',error_code='engine_transport_unknown',"
            "updated_at_us=%s,version=version+1 WHERE submit_intent_id='submit-test'",
            (redacted_at_us, "sha256:" + "a" * 64, redacted_at_us),
        )
        result = query.query_outcome("submit-test")
        assert result is not None
        recovery = cast(dict[str, Any], result.status()["abort_recovery"])
        assert recovery["document_state"] == "redacted"
        assert recovery["execution_request_digest"] == db.request.request_digest
        assert recovery["receipt"] is not None
        assert "delete-test" not in repr(recovery)
        db.sql(
            "UPDATE admission_leases SET lease_state='reserved' "
            "WHERE lease_id='lease-test-stage-main'"
        )
        with pytest.raises(SubmissionStoreError):
            query.query_outcome("submit-test")
    finally:
        db.sql(
            "UPDATE admission_leases SET lease_state=%s WHERE lease_id='lease-test-stage-main'",
            (lease["lease_state"],),
        )
        db.sql(
            "UPDATE submit_intents SET request_document=%s,command_redacted_at_us=NULL,"
            "command_deletion_job_id=NULL,command_deletion_evidence_digest=NULL,"
            "execution_state=%s,cleanup_state=%s,error_code=%s,updated_at_us=%s,version=%s "
            "WHERE submit_intent_id='submit-test'",
            (
                before["request_document"],
                before["execution_state"],
                before["cleanup_state"],
                before["error_code"],
                before["updated_at_us"],
                before["version"],
            ),
        )
        db.sql("DELETE FROM gateway_abort_intents WHERE submit_intent_id='submit-test'")
