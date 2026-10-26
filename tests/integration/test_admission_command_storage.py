"""Private admission storage, real lock waits and rollback; no scheduler qualification."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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

from speechnexus_backend.speech.infrastructure.jobs_sql import create_in_transaction

pytestmark = pytest.mark.integration
PRIVATE_BODY = '{"synthetic_effective_command":true}'
BUSINESS_DIGEST = "sha256:" + hashlib.sha256(PRIVATE_BODY.encode()).hexdigest()
COMMANDS = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures/coordination/transaction-examples.json"
    ).read_text()
)
ADMISSION_BODY = json.dumps(
    next(c["request"] for c in COMMANDS["valid_commands"] if c["operation"] == "admit_recipe"),
    sort_keys=True,
    separators=(",", ":"),
)
ADMISSION_DIGEST = "sha256:" + hashlib.sha256(ADMISSION_BODY.encode()).hexdigest()
OPERATION_SQL = (
    "INSERT INTO admission_operations (operation_id, workspace_id, request_digest, "
    "request_document, initial_attempt_id, current_attempt_id, recorded_at_us) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
OPERATION_VALUES = (
    "operation-test",
    "workspace-test",
    BUSINESS_DIGEST,
    PRIVATE_BODY,
    "attempt-test",
    "attempt-test",
    1,
)
ADMISSION_SQL = (
    "INSERT INTO admission_requests (operation_id, attempt_id, request_digest, request_document, "
    "recorded_at_us) VALUES (%s, %s, %s, %s, %s)"
)
ADMISSION_VALUES = ("operation-test", "attempt-test", ADMISSION_DIGEST, ADMISSION_BODY, 1)


def _manual_transaction(connection: Any, dialect: str) -> None:
    if dialect == "postgresql":
        connection.autocommit = False
    else:
        connection.autocommit(False)


def _wait_for_command_lock(foundation: Foundation) -> None:
    db = foundation.database
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if db.dialect == "postgresql":
            rows = db.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = %s "
                "AND cardinality(pg_blocking_pids(pid)) > 0",
                (foundation.speech_role,),
            )
        else:
            rows = db.query(
                "SELECT count(*) AS n FROM performance_schema.data_lock_waits w "
                "JOIN performance_schema.data_locks l "
                "ON w.REQUESTING_ENGINE_LOCK_ID = l.ENGINE_LOCK_ID "
                "WHERE l.OBJECT_SCHEMA = %s AND l.OBJECT_NAME = 'admission_operations'",
                (db.schema,),
            )
        if rows[0]["n"]:
            return
        time.sleep(0.01)
    pytest.fail("No actual admission operation lock wait was observed")


def test_command_and_envelope_are_separate_private_records(foundation: Foundation) -> None:
    db = foundation.database
    with foundation.connection("speech") as connection, connection.cursor() as cursor:
        cursor.execute(OPERATION_SQL, OPERATION_VALUES)
        cursor.execute(ADMISSION_SQL, ADMISSION_VALUES)
    operation = db.query("SELECT * FROM admission_operations")[0]
    admission = db.query("SELECT * FROM admission_requests")[0]
    assert operation["request_document"] == PRIVATE_BODY
    assert operation["request_digest"] == BUSINESS_DIGEST != admission["request_digest"]
    assert admission["request_document"] == ADMISSION_BODY
    assert admission["request_digest"] == ADMISSION_DIGEST
    assert operation["initial_attempt_id"] == operation["current_attempt_id"] == "attempt-test"
    assert operation["version"] == 1
    assert operation["command_redacted_at_us"] is None
    # Storage is not a public Operation/receipt or a new query_outcome branch.
    assert db.counts() == (0, 0)


def test_private_business_body_is_retained_without_64k_truncation(foundation: Foundation) -> None:
    body = json.dumps({"synthetic_text": "z" * 70000}, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    values = list(OPERATION_VALUES)
    values[2:4] = [digest, body]
    with foundation.connection("speech") as connection, connection.cursor() as cursor:
        cursor.execute(OPERATION_SQL, tuple(values))
    assert foundation.database.query(
        "SELECT request_document, request_digest FROM admission_operations"
    ) == [{"request_document": body, "request_digest": digest}]


@pytest.mark.parametrize(
    "document,recorded", [("", 1), (None, 1), (ADMISSION_BODY, 0), (ADMISSION_BODY, 2**53)]
)
def test_admission_envelope_requires_body_and_bounded_creation_time(
    foundation: Foundation, document: str | None, recorded: int
) -> None:
    db = foundation.database
    db.query(OPERATION_SQL, OPERATION_VALUES)
    with pytest.raises((psycopg.IntegrityError, pymysql.MySQLError)):
        db.query(ADMISSION_SQL, (*ADMISSION_VALUES[:3], document, recorded))
    assert db.query("SELECT count(*) AS n FROM admission_requests")[0]["n"] == 0


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM admission_operations",
        "SELECT operation_id FROM admission_operations",
        "SELECT request_document FROM admission_requests",
        "UPDATE admission_operations SET version = version + 1",
        "UPDATE admission_operations SET request_document = 'other'",
        "INSERT INTO admission_operations SELECT * FROM admission_operations",
        "DELETE FROM admission_requests",
    ],
)
def test_gateway_has_no_private_command_or_operation_lock_authority(
    foundation: Foundation, statement: str
) -> None:
    with (
        foundation.connection("gateway") as connection,
        connection.cursor() as cursor,
        pytest.raises((psycopg.errors.InsufficientPrivilege, pymysql.OperationalError)),
    ):
        cursor.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE admission_operations SET operation_id = 'other-operation'",
        "UPDATE admission_operations SET workspace_id = 'other-workspace'",
        "UPDATE admission_operations SET request_digest = 'other-digest'",
        "UPDATE admission_operations SET initial_attempt_id = 'other-attempt'",
        "UPDATE admission_operations SET recorded_at_us = 2",
        "UPDATE admission_requests SET request_document = 'other'",
        "UPDATE admission_requests SET request_digest = 'other-digest'",
        "UPDATE admission_requests SET attempt_id = 'other-attempt'",
        "DELETE FROM admission_operations",
        "DELETE FROM admission_requests",
        "ALTER TABLE admission_operations ADD COLUMN unauthorized BIGINT",
    ],
)
def test_speech_cannot_rewrite_immutable_commands_or_delete_history(
    foundation: Foundation, statement: str
) -> None:
    with foundation.connection("speech") as connection, connection.cursor() as cursor:
        cursor.execute(OPERATION_SQL, OPERATION_VALUES)
        cursor.execute(ADMISSION_SQL, ADMISSION_VALUES)
        with pytest.raises((psycopg.errors.InsufficientPrivilege, pymysql.OperationalError)):
            cursor.execute(statement)


@pytest.mark.parametrize(
    "assignment",
    [
        "request_document = NULL",
        "request_document = ''",
        "command_redacted_at_us = 2",
        "command_deletion_job_id = 'deletion-test'",
        "command_deletion_evidence_digest = request_digest",
        "request_document = NULL, command_redacted_at_us = 2, "
        "command_deletion_job_id = 'deletion-test'",
        "request_document = NULL, command_redacted_at_us = 0, "
        "command_deletion_job_id = 'deletion-test', "
        "command_deletion_evidence_digest = request_digest",
        "request_document = NULL, command_redacted_at_us = 9007199254740992, "
        "command_deletion_job_id = 'deletion-test', "
        "command_deletion_evidence_digest = request_digest",
        "request_document = NULL, command_redacted_at_us = 2, "
        "command_deletion_job_id = '', command_deletion_evidence_digest = request_digest",
        "version = 0",
    ],
)
def test_command_body_and_complete_tombstone_are_mutually_exclusive(
    foundation: Foundation, assignment: str
) -> None:
    db = foundation.database
    db.query(OPERATION_SQL, OPERATION_VALUES)
    with pytest.raises((psycopg.IntegrityError, pymysql.MySQLError)):
        db.query("UPDATE admission_operations SET " + assignment)
    assert db.query("SELECT request_document, version FROM admission_operations") == [
        {"request_document": PRIVATE_BODY, "version": 1}
    ]


def test_tombstone_retains_identity_digests_and_admission_metadata(foundation: Foundation) -> None:
    db = foundation.database
    with foundation.connection("speech") as connection, connection.cursor() as cursor:
        cursor.execute(OPERATION_SQL, OPERATION_VALUES)
        cursor.execute(ADMISSION_SQL, ADMISSION_VALUES)
        # Storage shape only. A07 must supply deletion authorization and locked
        # execution/cleanup evidence before using this owner-only capability.
        cursor.execute(
            "UPDATE admission_operations SET request_document = NULL, command_redacted_at_us = 2, "
            "command_deletion_job_id = 'deletion-test', command_deletion_evidence_digest = %s, "
            "version = version + 1 WHERE operation_id = 'operation-test' AND version = 1",
            (BUSINESS_DIGEST,),
        )
        assert cursor.rowcount == 1
    operation = db.query("SELECT * FROM admission_operations")[0]
    assert operation["request_document"] is None
    assert operation["request_digest"] == BUSINESS_DIGEST
    assert operation["recorded_at_us"] == 1 and operation["command_redacted_at_us"] == 2
    assert operation["initial_attempt_id"] == "attempt-test" and operation["version"] == 2
    assert db.query("SELECT request_document FROM admission_requests") == [
        {"request_document": ADMISSION_BODY}
    ]
    assert db.query("SELECT count(*) AS n FROM admission_attempts")[0]["n"] == 1


@pytest.mark.parametrize("missing", ["operation", "attempt"])
def test_envelope_requires_both_original_authorities(foundation: Foundation, missing: str) -> None:
    db = foundation.database
    if missing == "attempt":
        db.query(OPERATION_SQL, OPERATION_VALUES)
    values = list(ADMISSION_VALUES)
    if missing == "attempt":
        values[1] = "absent-attempt"
    with pytest.raises((psycopg.IntegrityError, pymysql.MySQLError)):
        db.query(ADMISSION_SQL, tuple(values))
    assert db.query("SELECT count(*) AS n FROM admission_requests")[0]["n"] == 0


def test_shared_operation_id_serializes_different_attempts_and_workspaces(
    foundation: Foundation,
) -> None:
    db = foundation.database

    def competing_insert() -> None:
        with foundation.connection("speech") as connection, connection.cursor() as cursor:
            other = list(OPERATION_VALUES)
            other[1], other[4], other[5] = "other-workspace", "other-attempt", "other-attempt"
            cursor.execute(OPERATION_SQL, tuple(other))

    with foundation.connection("speech") as first, ThreadPoolExecutor(max_workers=1) as executor:
        _manual_transaction(first, db.dialect)
        with first.cursor() as cursor:
            cursor.execute(OPERATION_SQL, OPERATION_VALUES)
        future = executor.submit(competing_insert)
        try:
            _wait_for_command_lock(foundation)
        finally:
            first.commit()
        with pytest.raises((psycopg.IntegrityError, pymysql.MySQLError)):
            future.result(timeout=5)
    assert db.query("SELECT workspace_id, current_attempt_id FROM admission_operations") == [
        {"workspace_id": "workspace-test", "current_attempt_id": "attempt-test"}
    ]


def test_operation_lock_can_protect_a_future_attempt_transition(foundation: Foundation) -> None:
    db = foundation.database
    db.query(OPERATION_SQL, OPERATION_VALUES)

    def competing_update() -> int:
        with foundation.connection("speech") as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admission_operations SET current_attempt_id = 'next-attempt', "
                "version = version + 1 WHERE operation_id = 'operation-test' AND version = 1"
            )
            return cast(int, cursor.rowcount)

    with foundation.connection("speech") as first, ThreadPoolExecutor(max_workers=1) as executor:
        _manual_transaction(first, db.dialect)
        with first.cursor() as cursor:
            cursor.execute(
                "SELECT operation_id FROM admission_operations "
                "WHERE operation_id = 'operation-test' FOR UPDATE"
            )
        future = executor.submit(competing_update)
        try:
            _wait_for_command_lock(foundation)
            with first.cursor() as cursor:
                cursor.execute(
                    "UPDATE admission_operations SET version = version + 1 "
                    "WHERE operation_id = 'operation-test' AND version = 1"
                )
        finally:
            first.commit()
        assert future.result(timeout=5) == 0
    # The storage permits a versioned transition; it does not authorize retry.
    assert db.query("SELECT current_attempt_id, version FROM admission_operations") == [
        {"current_attempt_id": "attempt-test", "version": 2}
    ]


def test_private_records_and_job_outbox_roll_back_together(foundation: Foundation) -> None:
    db = foundation.database
    with foundation.connection("speech") as connection:
        _manual_transaction(connection, db.dialect)
        try:
            with connection.cursor() as cursor:
                cursor.execute(OPERATION_SQL, OPERATION_VALUES)
                cursor.execute(ADMISSION_SQL, ADMISSION_VALUES)
                assert create_in_transaction(cursor, REQUEST).outcome == "committed"
                raise RuntimeError("synthetic failure after all writes")
        except RuntimeError:
            connection.rollback()
    assert db.counts() == (0, 0)
    assert db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"] == 0
    assert db.query("SELECT count(*) AS n FROM admission_requests")[0]["n"] == 0
    # The preexisting legacy attempt remains; no fake command was attached.
    assert db.query("SELECT count(*) AS n FROM admission_attempts")[0]["n"] == 1


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_0007_failure_rolls_back_and_upgrade_preserves_legacy_commands_and_acl(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0006_runtime_observation")

    class SeedDatabase:
        def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
            sandbox.admin.execute(statement, parameters)

    seed(cast(JobDatabase, SeedDatabase()))
    original = sandbox.admin.execute("SELECT * FROM admission_attempts").fetchall()
    submits = sandbox.admin.execute("SELECT * FROM submit_intents").fetchall()
    acl_sql = "SELECT relacl FROM pg_class WHERE oid = 'admission_attempts'::regclass"
    acl = sandbox.admin.execute(acl_sql).fetchone()
    sandbox.admin.execute("CREATE TABLE admission_requests (sentinel BIGINT)")
    with pytest.raises(ProgrammingError) as error:
        sandbox.upgrade()
    assert getattr(error.value.orig, "sqlstate", None) == "42P07"
    assert sandbox.version() == "0006_runtime_observation"
    assert sandbox.admin.execute(
        "SELECT to_regclass('admission_operations') AS name"
    ).fetchone() == {"name": None}
    sandbox.admin.execute("DROP TABLE admission_requests")
    sandbox.upgrade()
    sandbox.upgrade()
    assert sandbox.version() == "0014_pronunciation_drafts"
    assert sandbox.admin.execute("SELECT * FROM admission_attempts").fetchall() == original
    assert sandbox.admin.execute("SELECT * FROM submit_intents").fetchall() == [
        row | {"staging_result_document": None} for row in submits
    ]
    assert sandbox.admin.execute(acl_sql).fetchone() == acl
    assert sandbox.admin.execute("SELECT * FROM admission_operations").fetchall() == []
    assert sandbox.admin.execute("SELECT * FROM admission_requests").fetchall() == []
