"""Runtime storage constraints and grants; not controller or scheduler qualification."""

from __future__ import annotations

from typing import Any, cast

import psycopg
import pymysql
import pytest
from sqlalchemy.exc import ProgrammingError
from tests.integration.test_coordination_foundation import Foundation, UpgradeSandbox, seed
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_coordination_foundation import upgrade_sandbox as upgrade_sandbox
from tests.integration.test_production_jobs import REQUEST, JobDatabase
from tests.integration.test_production_jobs import database as database

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "a" * 64
EVENT_SQL = (
    "INSERT INTO coordination_outbox "
    "(event_id, event_digest, event_kind, job_id, target_node_id, intent_id) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)


def _integrity(
    foundation: Foundation,
    statement: str,
    parameters: tuple[Any, ...],
    *,
    pgstate: str = "23514",
    mysqlcode: int = 3819,
) -> None:
    with pytest.raises((psycopg.IntegrityError, pymysql.MySQLError)) as error:
        foundation.database.query(statement, parameters)
    if foundation.database.dialect == "postgresql":
        assert getattr(error.value, "sqlstate", None) == pgstate
    else:
        assert error.value.args[0] == mysqlcode


def test_job_and_runtime_events_have_separate_valid_targets(foundation: Foundation) -> None:
    db = foundation.database
    assert db.port.create_job(REQUEST).outcome == "committed"
    assert db.query(
        "SELECT event_kind, job_id, target_node_id, intent_id FROM coordination_outbox"
    ) == [
        {"event_kind": "job", "job_id": REQUEST.job_id, "target_node_id": None, "intent_id": None}
    ]
    # Synthetic seed facts only; no controller observation or runtime admission.
    with foundation.connection("speech") as connection, connection.cursor() as cursor:
        cursor.execute(
            EVENT_SQL, ("event-runtime", DIGEST, "runtime", None, "host-test", "runtime-test")
        )
        cursor.execute(
            "UPDATE coordination_outbox SET delivered_at_us = 1 WHERE event_id = 'event-runtime'"
        )
        assert cursor.rowcount == 1
    assert db.counts() == (1, 2)
    assert db.port.create_job(REQUEST).outcome == "replayed"
    assert db.query(
        "SELECT job_id, target_node_id, intent_id FROM coordination_outbox WHERE event_id = %s",
        ("event-runtime",),
    ) == [{"job_id": None, "target_node_id": "host-test", "intent_id": "runtime-test"}]


@pytest.mark.parametrize(
    "kind,job,node,intent",
    [
        ("job", None, None, None),
        ("job", "job-test", "host-test", "runtime-test"),
        ("runtime", "job-test", "host-test", "runtime-test"),
        ("runtime", None, None, "runtime-test"),
        ("runtime", None, "host-test", None),
        ("other", "job-test", None, None),
    ],
)
def test_incomplete_mixed_or_unknown_targets_are_rejected(
    foundation: Foundation,
    kind: str,
    job: str | None,
    node: str | None,
    intent: str | None,
) -> None:
    foundation.database.port.create_job(REQUEST)
    _integrity(foundation, EVENT_SQL, ("event-invalid", DIGEST, kind, job, node, intent))
    assert foundation.database.counts() == (1, 1)


def test_runtime_target_foreign_key_is_node_scoped(foundation: Foundation) -> None:
    _integrity(
        foundation,
        EVENT_SQL,
        ("event-invalid", DIGEST, "runtime", None, "other-host", "runtime-test"),
        pgstate="23503",
        mysqlcode=1452,
    )
    _integrity(
        foundation,
        EVENT_SQL,
        ("event-invalid", DIGEST, "runtime", None, "host-test", "absent-intent"),
        pgstate="23503",
        mysqlcode=1452,
    )


def test_legacy_intents_keep_missing_facts_and_new_records_require_consistent_time(
    foundation: Foundation,
) -> None:
    db = foundation.database
    assert db.query(
        "SELECT recorded_at_us, updated_at_us, observation_document FROM runtime_instance_intents"
    ) == [{"recorded_at_us": None, "updated_at_us": None, "observation_document": None}]
    for recorded, updated, observation in (
        (1, None, None),
        (None, 1, None),
        (None, None, "{}"),
        (0, 1, None),
        (2, 1, None),
        (1, 2**53, None),
    ):
        _integrity(
            foundation,
            "UPDATE runtime_instance_intents SET recorded_at_us = %s, updated_at_us = %s, "
            "observation_document = %s",
            (recorded, updated, observation),
        )
    db.query("UPDATE runtime_instance_intents SET recorded_at_us = 1, updated_at_us = 1")
    with foundation.connection("speech") as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE runtime_instance_intents SET updated_at_us = 2, observation_document = '{}' "
        )
        assert cursor.rowcount == 1
    assert db.query("SELECT recorded_at_us, updated_at_us FROM runtime_instance_intents") == [
        {"recorded_at_us": 1, "updated_at_us": 2}
    ]


def test_roles_cannot_rewrite_creation_target_or_read_foreign_observation(
    foundation: Foundation,
) -> None:
    db = foundation.database
    db.port.create_job(REQUEST)
    db.query("UPDATE runtime_instance_intents SET recorded_at_us = 1, updated_at_us = 1")
    for owner, statement in (
        ("speech", "UPDATE runtime_instance_intents SET recorded_at_us = 2, updated_at_us = 2"),
        ("speech", "UPDATE coordination_outbox SET event_kind = 'runtime'"),
        ("speech", "UPDATE coordination_outbox SET target_node_id = 'other-host'"),
        ("speech", "UPDATE coordination_outbox SET intent_id = 'other-intent'"),
        ("gateway", "SELECT observation_document FROM runtime_instance_intents"),
        ("gateway", "UPDATE runtime_instance_intents SET updated_at_us = 2"),
    ):
        with foundation.connection(owner) as connection, connection.cursor() as cursor:
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, pymysql.OperationalError)
            ) as error:
                cursor.execute(statement)
            if db.dialect == "mysql":
                assert error.value.args[0] in {1142, 1143}


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_0005_failure_rolls_back_then_upgrade_preserves_legacy_rows_and_grants(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0004_asset_storage")

    class SeedDatabase:
        def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
            sandbox.admin.execute(statement, parameters)

    seed(cast(JobDatabase, SeedDatabase()))
    original = sandbox.admin.execute(
        "SELECT target_node_id, intent_id, request_document, intent_state, version "
        "FROM runtime_instance_intents"
    ).fetchall()
    acl = sandbox.admin.execute(
        "SELECT relacl FROM pg_class WHERE oid = 'coordination_outbox'::regclass"
    ).fetchone()
    column_acl_sql = (
        "SELECT attname, attacl FROM pg_attribute WHERE attrelid = 'coordination_outbox'::regclass "
        "AND attname IN ('event_id', 'job_id', 'event_digest', 'delivered_at_us') ORDER BY attname"
    )
    column_acl = sandbox.admin.execute(column_acl_sql).fetchall()
    sandbox.admin.execute("ALTER TABLE coordination_outbox ADD COLUMN event_kind VARCHAR(16)")
    with pytest.raises(ProgrammingError) as error:
        sandbox.upgrade()
    assert getattr(error.value.orig, "sqlstate", None) == "42701"
    assert sandbox.version() == "0004_asset_storage"
    assert (
        sandbox.admin.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = 'runtime_instance_intents' AND column_name = 'recorded_at_us'",
            (sandbox.schema,),
        ).fetchall()
        == []
    )
    sandbox.admin.execute("ALTER TABLE coordination_outbox DROP COLUMN event_kind")
    sandbox.upgrade()
    sandbox.upgrade()
    assert sandbox.version() == "0009_profile_drafts"
    assert (
        sandbox.admin.execute(
            "SELECT target_node_id, intent_id, request_document, intent_state, version "
            "FROM runtime_instance_intents"
        ).fetchall()
        == original
    )
    assert sandbox.admin.execute(
        "SELECT recorded_at_us, updated_at_us, observation_document FROM runtime_instance_intents"
    ).fetchall() == [{"recorded_at_us": None, "updated_at_us": None, "observation_document": None}]
    assert sandbox.admin.execute(
        "SELECT event_kind, job_id FROM coordination_outbox"
    ).fetchall() == [{"event_kind": "job", "job_id": REQUEST.job_id}]
    assert (
        sandbox.admin.execute(
            "SELECT relacl FROM pg_class WHERE oid = 'coordination_outbox'::regclass"
        ).fetchone()
        == acl
    )
    assert sandbox.admin.execute(column_acl_sql).fetchall() == column_acl
