"""Isolated PG: consistent abort recovery and the independent reader's real ACLs.

Every object this module grants on belongs to the module's own disposable database
(``sn_abort_reader_<hex>``, ``TEMPLATE template0``), so PostgreSQL's per-database
catalogs are already private to one sandbox: ``pg_class`` and the ``relacl``/``attacl``
rows of the tables below, ``pg_settings``, ``pg_namespace`` for ``pg_catalog``, and
locally created functions and sequences. Two catalogs are cluster-wide and cannot be
per-database — ``pg_authid`` and ``pg_parameter_acl`` — so nothing here writes them;
`_SENSITIVE_CATALOG` and the ALTER SYSTEM case below carry the per-sandbox equivalents
that keep the audited predicate intact.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
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
from tests.support.portable_pg import connect as portable_pg_connect

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
# A per-database system catalog whose initial privileges are not PUBLIC, standing in
# for the cluster-wide `pg_authid`: a reader that can read either one is wider than its
# surface. `pg_statistic` is chosen because writing its ACL touches only this
# sandbox's own `pg_class` row, while `pg_authid`'s row lives in the shared catalog and
# is rewritten for the whole cluster.
_SENSITIVE_CATALOG = "pg_catalog.pg_statistic"


@dataclass
class ReaderDatabase:
    url: str
    schema: str
    admin: Any
    request: SubmitRequest

    def sql(
        self, query: str | sql.Composable, params: tuple[object, ...] = ()
    ) -> list[dict[str, Any]]:
        cursor = self.admin.execute(query, params)
        return list(cursor.fetchall()) if cursor.description else []


def _sandbox_suffix(db: ReaderDatabase) -> str:
    """This sandbox's own name suffix, taken from its disposable database's schema."""
    return db.schema.removeprefix("sn_reader_")


def _sandbox_object(db: ReaderDatabase, stem: str) -> str:
    """Name a sandbox-owned object so two sandboxes can never name the same one."""
    return f"{stem}_{_sandbox_suffix(db)}"


def _sandbox_parameter(db: ReaderDatabase) -> str:
    """A parameter whose `pg_parameter_acl` row only this sandbox writes.

    `pg_parameter_acl` is a shared catalog with a unique index on the parameter name, so
    a real GUC is one row for the whole cluster; see the ALTER SYSTEM case.
    """
    return f"{db.schema}.alter_system"


@contextmanager
def _refused_while(
    db: ReaderDatabase,
    pool: GatewayPool,
    *,
    escalate: Sequence[Any],
    revert: Sequence[Any],
) -> Iterator[None]:
    """Hold one escalation open while the audit is asked to refuse the reader.

    `verify_execution_reader_authority` answers with a boolean and fails closed on every
    exception, so a bare ``assert not`` cannot tell "the audit saw this escalation" from
    "the pool or a catalog broke". This window pins the refusal to the escalation from
    both ends: the audit is green immediately before it (the fixture itself is not wider
    than the reader, #888) and green again once it is reverted (so the refusal belonged
    to what was applied in between, not to leftover state or a failed connection).
    """
    assert verify_execution_reader_authority(pool, db.schema) is True, (
        "the reader is not authorised before the escalation, so a refusal could not be "
        "attributed to it"
    )
    try:
        for statement in escalate:
            db.admin.execute(statement)
        yield
    finally:
        for statement in revert:
            db.admin.execute(statement)
    assert verify_execution_reader_authority(pool, db.schema) is True, (
        "reverting the escalation did not restore the reader's authority"
    )


@pytest.fixture(scope="module")
def reader_database() -> Iterator[ReaderDatabase]:
    root_url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not root_url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not evidence"
        )
    database_name = "sn_abort_reader_" + uuid.uuid4().hex[:12]
    schema = "sn_reader_" + uuid.uuid4().hex[:12]
    root = portable_pg_connect(root_url, autocommit=True)
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
            creator=lambda: portable_pg_connect(url),
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
        admin = portable_pg_connect(url, autocommit=True, row_factory=dict_row)
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
    # The receipt records the original instance generation, as the gateway does.
    db.sql(
        "INSERT INTO gateway_abort_intents "
        "(submit_intent_id,operation_id,attempt_id,fencing_token,reason,wait_timeout_ms,"
        "received_at_us,instance_id,instance_generation) "
        "SELECT 'submit-test','operation-test','attempt-test',1,'client_cancelled',1000,%s,"
        "l.instance_id,l.instance_generation FROM submit_intents AS s "
        "JOIN admission_leases AS l ON l.lease_id=s.lease_id "
        "WHERE s.submit_intent_id='submit-test'",
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
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege) as denied_write,
        pool.connection() as connection,
    ):
        connection.execute("UPDATE submit_intents SET execution_state='failed'")
    assert denied_write.value.diag.message_primary == "permission denied for table submit_intents"
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege) as denied_read,
        pool.connection() as connection,
    ):
        connection.execute("SELECT request_document FROM runtime_instance_intents")
    assert denied_read.value.diag.message_primary == (
        "permission denied for table runtime_instance_intents"
    )
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
        with portable_pg_connect(other_url, autocommit=True) as other:
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
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(group)),
            sql.SQL("GRANT UPDATE (execution_state) ON {}.submit_intents TO {}").format(
                sql.Identifier(db.schema), sql.Identifier(group)
            ),
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(db.schema), sql.Identifier(group)
            ),
            sql.SQL("GRANT {} TO {} WITH " + membership + ", ADMIN FALSE").format(
                sql.Identifier(group), sql.Identifier(role)
            ),
        ],
        revert=[
            sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(group), sql.Identifier(role)),
            sql.SQL("DROP OWNED BY {}").format(sql.Identifier(group)),
            sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(group)),
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.pg_has_role(current_user,%s,'MEMBER') AS reachable",
                (group,),
            ).fetchone() == {"reachable": True}
            if "SET TRUE" in membership:
                connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(group)))
            connection.execute("UPDATE submit_intents SET execution_state='submitting'")
        assert not verify_execution_reader_authority(pool, db.schema)


def test_public_temp_and_write_are_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    """PUBLIC escalations in this sandbox's own database are refused with their cause named.

    Each grant targets this sandbox's disposable database or schema, so the `relacl`,
    `attacl` and `datacl` rows rewritten here belong to one worker only.
    """
    db = reader_database
    _, pool = reader
    name = db.admin.info.dbname
    assert name is not None
    with _refused_while(
        db,
        pool,
        escalate=[sql.SQL("GRANT TEMPORARY ON DATABASE {} TO PUBLIC").format(sql.Identifier(name))],
        revert=[
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.has_database_privilege("
                "current_user,current_database(),'TEMPORARY') AS allowed"
            ).fetchone() == {"allowed": True}
        assert not verify_execution_reader_authority(pool, db.schema)
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("GRANT UPDATE (execution_state) ON {}.submit_intents TO PUBLIC").format(
                sql.Identifier(db.schema)
            )
        ],
        revert=[
            sql.SQL("REVOKE UPDATE (execution_state) ON {}.submit_intents FROM PUBLIC").format(
                sql.Identifier(db.schema)
            )
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.has_column_privilege("
                "current_user,'submit_intents','execution_state','UPDATE') AS allowed"
            ).fetchone() == {"allowed": True}
        assert not verify_execution_reader_authority(pool, db.schema)
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL(
                "GRANT SELECT (request_document) ON {}.runtime_instance_intents TO PUBLIC"
            ).format(sql.Identifier(db.schema))
        ],
        revert=[
            sql.SQL(
                "REVOKE SELECT (request_document) ON {}.runtime_instance_intents FROM PUBLIC"
            ).format(sql.Identifier(db.schema))
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.has_column_privilege("
                "current_user,'runtime_instance_intents','request_document','SELECT') AS allowed"
            ).fetchone() == {"allowed": True}
        assert not verify_execution_reader_authority(pool, db.schema)


@pytest.mark.parametrize("capability", ["pg_write_all_data", "pg_read_server_files"])
def test_predefined_role_even_without_set_is_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], capability: str
) -> None:
    db = reader_database
    role, pool = reader
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE").format(
                sql.Identifier(capability), sql.Identifier(role)
            )
        ],
        revert=[
            sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(capability), sql.Identifier(role))
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.pg_has_role(current_user,%s,'MEMBER') AS inherited",
                (capability,),
            ).fetchone() == {"inherited": True}
        assert not verify_execution_reader_authority(pool, db.schema)


def test_sequence_and_security_definer_bypasses_are_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    role, pool = reader
    sequence = _sandbox_object(db, "reader_test_sequence")
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("CREATE SEQUENCE {}.{}").format(
                sql.Identifier(db.schema), sql.Identifier(sequence)
            ),
            sql.SQL("GRANT USAGE ON SEQUENCE {}.{} TO {}").format(
                sql.Identifier(db.schema), sql.Identifier(sequence), sql.Identifier(role)
            ),
        ],
        revert=[
            sql.SQL("DROP SEQUENCE IF EXISTS {}.{}").format(
                sql.Identifier(db.schema), sql.Identifier(sequence)
            )
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.nextval(%s::pg_catalog.regclass) AS value",
                (f"{db.schema}.{sequence}",),
            ).fetchone() == {"value": 1}
        assert not verify_execution_reader_authority(pool, db.schema)
    marker = _sandbox_object(db, "reader_definer_marker")
    definer = _sandbox_object(db, "reader_test_definer")
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("CREATE TABLE {}.{} (value integer)").format(
                sql.Identifier(db.schema), sql.Identifier(marker)
            ),
            sql.SQL(
                "CREATE FUNCTION {}.{}() RETURNS integer LANGUAGE SQL SECURITY DEFINER AS {}"
            ).format(
                sql.Identifier(db.schema),
                sql.Identifier(definer),
                sql.Literal(f"INSERT INTO {db.schema}.{marker} VALUES (1) RETURNING value"),
            ),
        ],
        revert=[
            sql.SQL("DROP FUNCTION IF EXISTS {}.{}()").format(
                sql.Identifier(db.schema), sql.Identifier(definer)
            ),
            sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                sql.Identifier(db.schema), sql.Identifier(marker)
            ),
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                sql.SQL("SELECT {}.{}() AS value").format(
                    sql.Identifier(db.schema), sql.Identifier(definer)
                )
            ).fetchone() == {"value": 1}
        assert db.sql(
            sql.SQL("SELECT value FROM {}.{}").format(
                sql.Identifier(db.schema), sql.Identifier(marker)
            )
        ) == [{"value": 1}]
        assert not verify_execution_reader_authority(pool, db.schema)


def test_security_definer_outside_application_schema_is_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    _, pool = reader
    definer = _sandbox_object(db, "reader_test_definer")
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL(
                "CREATE FUNCTION information_schema.{}() RETURNS integer "
                "LANGUAGE SQL SECURITY DEFINER AS {}"
            ).format(sql.Identifier(definer), sql.Literal("SELECT 1"))
        ],
        revert=[
            sql.SQL("DROP FUNCTION IF EXISTS information_schema.{}()").format(
                sql.Identifier(definer)
            )
        ],
    ):
        assert not verify_execution_reader_authority(pool, db.schema)


def test_column_grant_option_cannot_delegate_private_command_access(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    """A column-level `WITH GRANT OPTION` is refused, and refused for that reason.

    The reader really can delegate the private `request_document` column while the
    option is in force — it performs the delegation itself — so the refusal cannot be
    read as "the grant did not apply".
    """
    db = reader_database
    role, pool = reader
    recipient = "sn_reader_extra_" + uuid.uuid4().hex[:12]
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(recipient)),
            sql.SQL(
                "GRANT SELECT (request_document) ON {}.submit_intents TO {} WITH GRANT OPTION"
            ).format(sql.Identifier(db.schema), sql.Identifier(role)),
        ],
        revert=[
            sql.SQL(
                "REVOKE GRANT OPTION FOR SELECT (request_document) ON {}.submit_intents "
                "FROM {} CASCADE"
            ).format(sql.Identifier(db.schema), sql.Identifier(role)),
            sql.SQL("DROP OWNED BY {}").format(sql.Identifier(recipient)),
            sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(recipient)),
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.has_column_privilege("
                "current_user,'submit_intents','request_document',"
                "'SELECT WITH GRANT OPTION') AS delegable"
            ).fetchone() == {"delegable": True}
            connection.execute(
                sql.SQL("GRANT SELECT (request_document) ON {}.submit_intents TO {}").format(
                    sql.Identifier(db.schema), sql.Identifier(recipient)
                )
            )
        assert not verify_execution_reader_authority(pool, db.schema)


@pytest.mark.parametrize("authority", ["schema_create", "table_update", "column_update"])
def test_catalog_create_and_write_privileges_are_never_trusted(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], authority: str
) -> None:
    """`pg_catalog` write authority in this database is refused, not just unexercised.

    `pg_namespace.nspacl` for `pg_catalog` and `pg_class`'s own `relacl`/`attacl` are
    per-database rows, so the grants below never reach another worker.
    """
    db = reader_database
    role, pool = reader
    permission = {
        "schema_create": "CREATE ON SCHEMA pg_catalog",
        "table_update": "UPDATE ON TABLE pg_catalog.pg_class",
        "column_update": "UPDATE (relpages) ON TABLE pg_catalog.pg_class",
    }[authority]
    with _refused_while(
        db,
        pool,
        escalate=[sql.SQL("GRANT " + permission + " TO {}").format(sql.Identifier(role))],
        revert=[sql.SQL("REVOKE " + permission + " FROM {}").format(sql.Identifier(role))],
    ):
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


def test_catalog_sensitive_read_and_public_settings_write_are_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    """Reading a non-PUBLIC system catalog or writing settings is refused, per surface.

    The sensitive catalog is this sandbox's own `pg_statistic`, not the cluster-wide
    `pg_authid`: `pg_authid` and its `pg_class` row live in the shared catalog, so a
    grant on it rewrites one row for every database in the cluster (a concurrent worker
    raises `tuple concurrently updated`) and widens every other sandbox's reader while
    it is in force. `pg_statistic` keeps the same audit verdict for the same reason —
    readable and not initially PUBLIC metadata — with an ACL row this database owns.
    """
    db = reader_database
    role, pool = reader
    for grantee in (sql.Identifier(role), sql.SQL("PUBLIC")):
        with _refused_while(
            db,
            pool,
            escalate=[
                sql.SQL("GRANT SELECT ON {} TO {}").format(sql.SQL(_SENSITIVE_CATALOG), grantee)
            ],
            revert=[
                sql.SQL("REVOKE SELECT ON {} FROM {}").format(sql.SQL(_SENSITIVE_CATALOG), grantee)
            ],
        ):
            with pool.connection() as connection:
                assert connection.execute(
                    "SELECT pg_catalog.has_table_privilege(current_user,%s,'SELECT') AS readable",
                    (_SENSITIVE_CATALOG,),
                ).fetchone() == {"readable": True}
            assert not verify_execution_reader_authority(pool, db.schema)
    with _refused_while(
        db,
        pool,
        escalate=["GRANT DELETE ON pg_catalog.pg_settings TO PUBLIC"],
        revert=["REVOKE DELETE ON pg_catalog.pg_settings FROM PUBLIC"],
    ):
        assert db.sql(
            "SELECT count(*) AS entries FROM pg_catalog.pg_class c "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) acl "
            "WHERE c.oid='pg_catalog.pg_settings'::pg_catalog.regclass "
            "AND acl.grantee=0 AND acl.privilege_type='DELETE'"
        ) == [{"entries": 1}]
        assert not verify_execution_reader_authority(pool, db.schema)
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("GRANT UPDATE ON pg_catalog.pg_settings TO {} WITH GRANT OPTION").format(
                sql.Identifier(role)
            )
        ],
        revert=[
            sql.SQL("REVOKE UPDATE ON pg_catalog.pg_settings FROM {}").format(sql.Identifier(role))
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.has_table_privilege("
                "current_user,'pg_catalog.pg_settings','UPDATE WITH GRANT OPTION') "
                "AS delegable"
            ).fetchone() == {"delegable": True}
        assert not verify_execution_reader_authority(pool, db.schema)
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("GRANT UPDATE (setting) ON pg_catalog.pg_settings TO {}").format(
                sql.Identifier(role)
            )
        ],
        revert=[
            sql.SQL("REVOKE UPDATE (setting) ON pg_catalog.pg_settings FROM {}").format(
                sql.Identifier(role)
            )
        ],
    ):
        # `pg_settings` carries an initial PUBLIC UPDATE, so `has_column_privilege`
        # answers true before this grant as well; the explicit entry in `attacl` is what
        # the grant actually adds.
        assert db.sql(
            "SELECT count(*) AS entries FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl "
            "JOIN pg_catalog.pg_roles r ON r.oid=acl.grantee "
            "WHERE c.oid='pg_catalog.pg_settings'::pg_catalog.regclass "
            "AND a.attname='setting' AND r.rolname=%s AND acl.privilege_type='UPDATE'",
            (role,),
        ) == [{"entries": 1}]
        assert not verify_execution_reader_authority(pool, db.schema)


def test_alter_system_parameter_privilege_is_rejected_without_changing_settings(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    """A `pg_parameter_acl` entry that lets the reader alter a server parameter is refused.

    `pg_parameter_acl` is a shared catalog with a unique index on `parname`, so a real
    GUC (`log_min_duration_statement`) is one row the whole cluster rewrites: two
    sandbox workers granting it at once raise `duplicate key value violates unique
    constraint "pg_parameter_acl_parname_index"` (`already exists`), and the row update
    itself raises `tuple concurrently updated`. The parameter is therefore this
    sandbox's own namespaced name, which PostgreSQL accepts for the same
    ``GRANT ALTER SYSTEM ON PARAMETER`` form and which only this worker's row holds.

    The audited predicate is unchanged: the audit walks every `pg_parameter_acl` row
    and refuses any reachable role with `has_parameter_privilege(..., 'ALTER SYSTEM')`.
    The refusal is pinned to this row: the reader holds nothing on the real GUC, the
    entry is the only row carrying this role, and the audit is green again once it is
    revoked.
    """
    db = reader_database
    role, pool = reader
    parameter = _sandbox_parameter(db)
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("GRANT ALTER SYSTEM ON PARAMETER {} TO {}").format(
                sql.SQL(parameter), sql.Identifier(role)
            )
        ],
        revert=[
            sql.SQL("REVOKE ALTER SYSTEM ON PARAMETER {} FROM {}").format(
                sql.SQL(parameter), sql.Identifier(role)
            )
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.has_parameter_privilege("
                "current_user,%s,'ALTER SYSTEM') AS allowed, "
                "pg_catalog.has_parameter_privilege("
                "current_user,'log_min_duration_statement','ALTER SYSTEM') AS real_guc",
                (parameter,),
            ).fetchone() == {"allowed": True, "real_guc": False}
        assert db.sql(
            "SELECT parname FROM pg_catalog.pg_parameter_acl "
            "WHERE parname=%s OR paracl::text LIKE %s",
            (parameter, f"%{role}%"),
        ) == [{"parname": parameter}]
        assert not verify_execution_reader_authority(pool, db.schema)
    assert (
        db.sql(
            "SELECT 1 AS leftover FROM pg_catalog.pg_parameter_acl WHERE parname=%s", (parameter,)
        )
        == []
    )


def test_catalog_security_definer_and_sequence_are_audited(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    """Objects created in this database's `pg_catalog` are audited, not just exercised.

    `pg_catalog` is a per-database namespace and the objects below carry this sandbox's
    suffix, so the only writer of these `pg_proc`/`pg_class` rows is this worker.
    """
    db = reader_database
    role, pool = reader
    definer = _sandbox_object(db, "sn_reader_test_definer")
    sequence = _sandbox_object(db, "sn_reader_test_sequence")
    db.sql("SET allow_system_table_mods=on")
    try:
        with _refused_while(
            db,
            pool,
            escalate=[
                sql.SQL(
                    "CREATE FUNCTION pg_catalog.{}() RETURNS integer LANGUAGE SQL "
                    "SECURITY DEFINER AS {}"
                ).format(sql.Identifier(definer), sql.Literal("SELECT 1"))
            ],
            revert=[
                sql.SQL("DROP FUNCTION IF EXISTS pg_catalog.{}()").format(sql.Identifier(definer))
            ],
        ):
            with pool.connection() as connection:
                assert connection.execute(
                    sql.SQL("SELECT pg_catalog.{}() AS value").format(sql.Identifier(definer))
                ).fetchone() == {"value": 1}
            assert not verify_execution_reader_authority(pool, db.schema)
        with _refused_while(
            db,
            pool,
            escalate=[
                sql.SQL("CREATE SEQUENCE pg_catalog.{}").format(sql.Identifier(sequence)),
                sql.SQL("GRANT USAGE ON SEQUENCE pg_catalog.{} TO {}").format(
                    sql.Identifier(sequence), sql.Identifier(role)
                ),
            ],
            revert=[
                sql.SQL("DROP SEQUENCE IF EXISTS pg_catalog.{}").format(sql.Identifier(sequence))
            ],
        ):
            with pool.connection() as connection:
                assert connection.execute(
                    "SELECT pg_catalog.nextval(%s::pg_catalog.regclass) AS value",
                    (f"pg_catalog.{sequence}",),
                ).fetchone() == {"value": 1}
            assert not verify_execution_reader_authority(pool, db.schema)
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
        created = sql.SQL("CREATE TYPE {} AS ENUM ('one')").format(object_identifier)
        owned = sql.SQL("ALTER TYPE {} OWNER TO {}").format(object_identifier, sql.Identifier(role))
        dropped = sql.SQL("DROP TYPE IF EXISTS {}").format(object_identifier)
    else:
        created = sql.SQL("CREATE DOMAIN {} AS text").format(object_identifier)
        owned = sql.SQL("ALTER DOMAIN {} OWNER TO {}").format(
            object_identifier, sql.Identifier(role)
        )
        dropped = sql.SQL("DROP DOMAIN IF EXISTS {}").format(object_identifier)
    with _refused_while(db, pool, escalate=[created, owned], revert=[dropped]):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_type t "
                "JOIN pg_catalog.pg_roles r ON r.oid=t.typowner "
                "WHERE t.typname=%s AND r.rolname=current_user) AS owns",
                (object_name,),
            ).fetchone() == {"owns": True}
        assert not verify_execution_reader_authority(pool, db.schema)


def test_public_large_object_writer_is_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    _, pool = reader
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege) as denied,
        pool.connection() as connection,
    ):
        connection.execute("SELECT pg_catalog.lo_create(0)")
    assert denied.value.diag.message_primary == "permission denied for function lo_create"
    created: dict[str, Any] = {}
    with _refused_while(
        db,
        pool,
        escalate=["GRANT EXECUTE ON FUNCTION pg_catalog.lo_create(oid) TO PUBLIC"],
        revert=["REVOKE EXECUTE ON FUNCTION pg_catalog.lo_create(oid) FROM PUBLIC"],
    ):
        with pool.connection() as connection:
            row = connection.execute("SELECT pg_catalog.lo_create(0) AS oid").fetchone()
            assert row is not None
            created = row
        assert not verify_execution_reader_authority(pool, db.schema)
        assert db.admin.execute("SELECT pg_catalog.lo_unlink(%s)", (created["oid"],)).fetchone()


@pytest.mark.parametrize(
    "authority", ["schema_create", "schema_owner", "relation_owner", "database_owner"]
)
def test_schema_and_database_or_relation_owners_are_rejected(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool], authority: str
) -> None:
    """Ownership of this sandbox's own schema, table or database is refused, with cause.

    Every object below belongs to this sandbox's disposable database, so the ownership
    change only rewrites a row one worker owns; the witness is the same predicate the
    audit reads (`pg_has_role(role, owner, 'MEMBER')`).
    """
    db = reader_database
    role, pool = reader
    current_owner = db.admin.execute("SELECT CURRENT_USER AS owner").fetchone()["owner"]
    database_name = db.admin.info.dbname
    assert database_name is not None
    schema = sql.Identifier(db.schema)
    escalate: list[Any]
    revert: list[Any]
    witness: tuple[str, tuple[Any, ...]]
    if authority == "schema_create":
        escalate = [sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(schema, sql.Identifier(role))]
        revert = [
            sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(schema, sql.Identifier(role))
        ]
        witness = (
            "SELECT pg_catalog.has_schema_privilege(current_user,%s,'CREATE') AS reached",
            (db.schema,),
        )
    elif authority == "schema_owner":
        escalate = [sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(schema, sql.Identifier(role))]
        # An ownership change rewrites the object's ACL (`aclnewowner`), so restoring the
        # owner alone leaves the reader without the grants the fixture gave it. The same
        # grant statements the fixture ran are re-applied, which is what makes the audit
        # green again — without it a "reverted" fixture would silently be narrower.
        revert = [
            sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(schema, sql.Identifier(current_owner)),
            *gateway_query_grants(db.schema, role),
        ]
        witness = (
            "SELECT pg_catalog.pg_has_role(current_user,n.nspowner,'MEMBER') AS reached "
            "FROM pg_catalog.pg_namespace n WHERE n.nspname=%s",
            (db.schema,),
        )
    elif authority == "relation_owner":
        escalate = [
            sql.SQL("ALTER TABLE {}.submit_intents OWNER TO {}").format(
                schema, sql.Identifier(role)
            )
        ]
        revert = [
            sql.SQL("ALTER TABLE {}.submit_intents OWNER TO {}").format(
                schema, sql.Identifier(current_owner)
            ),
            *gateway_query_grants(db.schema, role),
        ]
        witness = (
            "SELECT pg_catalog.pg_has_role(current_user,c.relowner,'MEMBER') AS reached "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relname='submit_intents' AND n.nspname=%s",
            (db.schema,),
        )
    else:
        escalate = [
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(database_name), sql.Identifier(role)
            )
        ]
        revert = [
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(database_name), sql.Identifier(current_owner)
            )
        ]
        witness = (
            "SELECT pg_catalog.pg_has_role(current_user,d.datdba,'MEMBER') AS reached "
            "FROM pg_catalog.pg_database d WHERE d.datname=current_database()",
            (),
        )
    with _refused_while(db, pool, escalate=escalate, revert=revert):
        with pool.connection() as connection:
            assert connection.execute(*witness).fetchone() == {"reached": True}
        assert not verify_execution_reader_authority(pool, db.schema)


def test_missing_column_and_row_security_fail_closed(
    reader_database: ReaderDatabase, reader: tuple[str, GatewayPool]
) -> None:
    db = reader_database
    role, pool = reader
    with _refused_while(
        db,
        pool,
        escalate=[
            sql.SQL("REVOKE SELECT (reason) ON {}.gateway_abort_intents FROM {}").format(
                sql.Identifier(db.schema), sql.Identifier(role)
            )
        ],
        revert=[
            sql.SQL("GRANT SELECT (reason) ON {}.gateway_abort_intents TO {}").format(
                sql.Identifier(db.schema), sql.Identifier(role)
            )
        ],
    ):
        with pool.connection() as connection:
            assert connection.execute(
                "SELECT pg_catalog.has_column_privilege("
                "current_user,'gateway_abort_intents','reason','SELECT') AS readable"
            ).fetchone() == {"readable": False}
        assert not verify_execution_reader_authority(pool, db.schema)
    with _refused_while(
        db,
        pool,
        escalate=["ALTER TABLE submit_intents ENABLE ROW LEVEL SECURITY"],
        revert=["ALTER TABLE submit_intents DISABLE ROW LEVEL SECURITY"],
    ):
        assert db.sql(
            "SELECT relrowsecurity FROM pg_catalog.pg_class "
            "WHERE oid='submit_intents'::pg_catalog.regclass"
        ) == [{"relrowsecurity": True}]
        assert not verify_execution_reader_authority(pool, db.schema)


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
