"""Actual PG authority and minimum-role InnoDB around identical binding transactions."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import patch

import psycopg
import pymysql
import pytest
from alembic import command as migration
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool
from tests.integration.test_coordination_foundation import UpgradeSandbox
from tests.integration.test_coordination_foundation import upgrade_sandbox as upgrade_sandbox
from tests.integration.test_production_jobs import ROOT, FaultConnection, JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.support.binding_database import BindingCursor, MySQLBindingDrafts
from tests.support.binding_examples import binding_request
from tests.support.job_database import MySQLJobTransactions

from speechnexus_backend.speech.application.binding_documents import (
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.application.binding_drafts import BindingDrafts
from speechnexus_backend.speech.domain.binding_drafts import (
    BindingUnavailableError,
    CreateBindingDraft,
)
from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.infrastructure import binding_drafts_sql as sql
from speechnexus_backend.speech.infrastructure.binding_draft_schema import (
    BINDING_DRAFT_DDL,
    binding_draft_grants,
)
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import (
    PostgresBindingDrafts,
    _Cursor,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.interfaces.binding_drafts import (
    draft_to_wire,
    prepare_binding_draft,
)

pytestmark = pytest.mark.integration
TABLES = ("speech_binding_commands", "speech_binding_families", "speech_binding_revisions")


@dataclass
class Bindings:
    db: JobDatabase
    port: BindingDrafts
    mysql: MySQLBindingDrafts | None = None

    def prepare(self, key: str = "original-key", **changes: Any) -> CreateBindingDraft:
        return prepare_binding_draft({**binding_request(), **changes}, key)

    def snapshot(self) -> dict[str, Any]:
        return {table: self.db.query(f"SELECT * FROM {table} ORDER BY 1,2") for table in TABLES}

    @contextmanager
    def writer(self) -> Iterator[tuple[Any, Any]]:
        if self.mysql is not None:
            mysql_connection = self.mysql.connector.connect()
            try:
                with mysql_connection.cursor() as mysql_raw:
                    yield mysql_connection, BindingCursor(mysql_raw)
                mysql_connection.commit()
            except BaseException:
                mysql_connection.rollback()
                raise
            finally:
                mysql_connection.close()
        else:
            assert self.db.pool is not None
            with self.db.pool.connection() as pg_connection, pg_connection.cursor() as pg_raw:
                yield pg_connection, _Cursor(pg_raw, time.monotonic() + 3)

    def waiters(self, minimum: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.db.dialect == "postgresql":
                rows = self.db.query(
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                    "AND cardinality(pg_blocking_pids(pid))>0",
                    (self.db.role,),
                )
            else:
                rows = self.db.query(
                    "SELECT count(*) AS n FROM performance_schema.data_lock_waits w "
                    "JOIN performance_schema.data_locks l "
                    "ON w.REQUESTING_ENGINE_LOCK_ID=l.ENGINE_LOCK_ID WHERE l.OBJECT_SCHEMA=%s",
                    (self.db.schema,),
                )
            if rows[0]["n"] >= minimum:
                return
            time.sleep(0.01)
        pytest.fail("Actual binding database lock waits were not observed")


@pytest.fixture
def bindings(database: JobDatabase) -> Bindings:
    db = database
    # The generic database fixture starts with Job grants. A binding writer gets
    # none of those: this suite verifies only the new declaration's minimum role.
    if db.dialect == "postgresql":
        for table in ("coordination_jobs", "coordination_outbox"):
            db.query(f'REVOKE ALL PRIVILEGES ON "{db.schema}".{table} FROM "{db.role}"')
        for grant in binding_draft_grants(db.schema, db.role):
            db.query(grant)
        assert db.pool is not None
        return Bindings(db, PostgresBindingDrafts(db.pool))
    for table in ("coordination_jobs", "coordination_outbox"):
        db.query(f"REVOKE ALL PRIVILEGES ON `{db.schema}`.{table} FROM '{db.role}'@'%'")
    for ddl in BINDING_DRAFT_DDL:
        db.query(ddl.replace('COLLATE "C"', "COLLATE utf8mb4_0900_bin").replace("TEXT", "LONGTEXT"))
    for table in TABLES:
        db.query(f"GRANT SELECT, INSERT ON `{db.schema}`.{table} TO '{db.role}'@'%'")
    db.query(
        f"GRANT UPDATE (latest_revision) ON `{db.schema}`.speech_binding_families "
        f"TO '{db.role}'@'%'"
    )
    mysql = MySQLBindingDrafts(
        MySQLJobTransactions(db.url, db.schema, db.role, "synthetic_job_test")
    )
    return Bindings(db, mysql, mysql)


def test_append_read_replay_restart_keep_original_global_revision_identity(
    bindings: Bindings,
) -> None:
    command = bindings.prepare()
    first = bindings.port.create(command)
    assert first.outcome == "created" and first.draft is not None
    second = bindings.port.create(bindings.prepare("another-key", supersedes_revision="1"))
    assert second.outcome == "created" and second.draft is not None and second.draft.revision == "2"
    assert first.draft.revision_id != second.draft.revision_id
    assert draft_to_wire(first.draft)["publication"]["status"] == "draft"
    before = bindings.snapshot()
    assert bindings.port.create(command).draft == first.draft
    assert bindings.port.recover(command).draft == first.draft
    rebuilt: BindingDrafts = (
        MySQLBindingDrafts(bindings.mysql.connector)
        if bindings.mysql is not None
        else PostgresBindingDrafts(cast(SpeechPool, bindings.db.pool))
    )
    assert rebuilt.get(command.binding_id, "1") == first.draft
    assert rebuilt.get(command.binding_id, "99") is None
    assert bindings.snapshot() == before and bindings.db.counts() == (0, 0)


def test_original_key_conflict_across_fields_and_families_keeps_original_journal(
    bindings: Bindings,
) -> None:
    command = bindings.prepare()
    assert bindings.port.create(command).outcome == "created"
    before = bindings.snapshot()
    for changes in (
        {"binding_id": "other-family"},
        {"logical_model": "different-model"},
        {"supersedes_revision": "1"},
    ):
        changed = bindings.prepare(**changes)
        for result in (bindings.port.create(changed), bindings.port.recover(changed)):
            assert result.outcome == "conflict" and result.error_code == "idempotency_conflict"
        assert bindings.snapshot() == before
    assert (
        bindings.port.recover(bindings.prepare("never-submitted-key")).recovery == "query_original"
    )
    assert bindings.snapshot() == before


def test_same_key_waits_for_original_commit_then_replays_without_allocating(
    bindings: Bindings,
) -> None:
    command = bindings.prepare()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with bindings.writer() as (_, cursor):
            first = sql.create_draft(cursor, command, cursor.clock, lambda: "mbrev-original")
            waiting = workers.submit(bindings.port.create, command)
            bindings.waiters(1)
            assert not waiting.done()
        replay = waiting.result(timeout=5)
    assert (
        first.outcome == "created" and replay.outcome == "replayed" and replay.draft == first.draft
    )
    assert len(bindings.snapshot()["speech_binding_revisions"]) == 1


def test_different_keys_wait_then_both_append_with_no_client_cas_rejection(
    bindings: Bindings,
) -> None:
    first, second = bindings.prepare("first-new-key"), bindings.prepare("second-new-key")
    bindings.db.query(
        "INSERT INTO speech_binding_families (binding_id) VALUES (%s)", (first.binding_id,)
    )
    blocker = bindings.db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT * FROM speech_binding_families FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(bindings.port.create, command) for command in (first, second)]
            try:
                bindings.waiters(2)
                assert not any(future.done() for future in futures)
            finally:
                blocker.rollback()
            results = [future.result(timeout=5) for future in futures]
    finally:
        blocker.rollback()
        blocker.close()
    assert all(result.outcome == "created" and result.draft is not None for result in results)
    assert {result.draft.revision for result in results if result.draft} == {"1", "2"}
    assert len({result.draft.revision_id for result in results if result.draft}) == 2
    before = bindings.snapshot()
    for command, result in zip((first, second), results, strict=True):
        assert bindings.port.recover(command).draft == result.draft
    assert bindings.snapshot() == before


@pytest.mark.parametrize("target", ["speech_binding_families"])
def test_failure_after_actual_update_rolls_back_all_binding_facts(
    bindings: Bindings, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    original = sql._update

    def fail(cursor: Any, statement: str, parameters: tuple[object, ...]) -> None:
        original(cursor, statement, parameters)
        if statement.startswith("UPDATE " + target):
            raise BindingUnavailableError

    monkeypatch.setattr(sql, "_update", fail)
    before = bindings.snapshot()
    command = bindings.prepare()
    assert bindings.port.create(command).recovery == "query_original"
    assert bindings.snapshot() == before
    assert bindings.port.recover(command).outcome == "unknown"
    assert bindings.snapshot() == before


def test_global_revision_id_collision_rolls_back_the_new_family_and_command(
    bindings: Bindings,
) -> None:
    def identity() -> str:
        return "mbrev-global-unique"

    if bindings.mysql is not None:
        bindings.mysql.identity_factory = identity
    else:
        bindings.port = PostgresBindingDrafts(
            cast(SpeechPool, bindings.db.pool), identity_factory=identity
        )
    first = bindings.port.create(bindings.prepare())
    assert first.draft is not None and first.draft.revision_id == identity()
    before = bindings.snapshot()
    second = bindings.prepare("other-family-key", binding_id="second-family")
    assert bindings.port.create(second).recovery == "query_original"
    assert bindings.snapshot() == before and bindings.port.recover(second).outcome == "unknown"
    assert bindings.port.get(first.draft.binding_id, first.draft.revision) == first.draft


def test_lost_actual_commit_then_native_query_pressure_recovers_original_identity(
    bindings: Bindings,
) -> None:
    command = bindings.prepare()
    if bindings.mysql is not None:
        bindings.mysql.lose_reply = True
        result = bindings.port.create(command)
    else:

        def configure(connection: FaultConnection) -> None:
            connection.execute(f'SET search_path TO "{bindings.db.schema}",pg_catalog')
            connection.commit()

        with ConnectionPool[FaultConnection](
            make_conninfo(bindings.db.url, user=bindings.db.role, password="synthetic_job_test"),
            connection_class=FaultConnection,
            kwargs={"row_factory": dict_row},
            configure=configure,
            min_size=1,
            max_size=1,
        ) as fault_pool:
            with fault_pool.connection() as connection:
                armed = connection
            armed.fault = "after"
            result = PostgresBindingDrafts(cast(SpeechPool, fault_pool)).create(command)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    before = bindings.snapshot()
    assert len(before["speech_binding_revisions"]) == 1
    if bindings.mysql is not None:
        blocker = bindings.db.blocker()
        try:
            blocker.cursor().execute("LOCK TABLES speech_binding_commands WRITE")
            original_connect = bindings.mysql.connector.connect

            def pressured() -> Any:
                connection = original_connect()
                connection.cursor().execute("SET SESSION lock_wait_timeout=1")
                return connection

            with patch.object(MySQLJobTransactions, "connect", lambda _: pressured()):
                assert bindings.port.recover(command).recovery == "query_original"
        finally:
            blocker.cursor().execute("UNLOCK TABLES")
            blocker.close()
    else:
        with (
            speech_pool(
                make_conninfo(
                    bindings.db.url, user=bindings.db.role, password="synthetic_job_test"
                ),
                schema=bindings.db.schema,
                max_size=1,
            ) as pressure_pool,
            pressure_pool.connection(),
        ):
            assert (
                PostgresBindingDrafts(pressure_pool, timeout_seconds=0.05).recover(command).recovery
                == "query_original"
            )
    assert bindings.snapshot() == before
    restored = bindings.port.recover(command)
    assert restored.outcome == "replayed" and restored.draft is not None
    assert restored.draft.revision_id == before["speech_binding_revisions"][0]["revision_id"]
    assert bindings.snapshot() == before and bindings.db.counts() == (0, 0)


def test_native_query_cancel_does_not_reallocate_or_change_the_original_command(
    bindings: Bindings,
) -> None:
    command = bindings.prepare()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with bindings.writer() as (_, cursor):
            original = sql.create_draft(
                cursor, command, cursor.clock, lambda: "mbrev-before-cancel"
            )
            pending = workers.submit(bindings.port.create, command)
            bindings.waiters(1)
            if bindings.db.dialect == "postgresql":
                rows = bindings.db.query(
                    "SELECT pid FROM pg_stat_activity WHERE usename=%s "
                    "AND cardinality(pg_blocking_pids(pid))>0",
                    (bindings.db.role,),
                )
                assert len(rows) == 1
                assert bindings.db.query(
                    "SELECT pg_cancel_backend(%s) AS cancelled", (rows[0]["pid"],)
                )[0]["cancelled"]
            else:
                rows = bindings.db.query(
                    "SELECT ID AS pid FROM information_schema.PROCESSLIST WHERE USER=%s "
                    "AND INFO LIKE 'INSERT%%speech_binding_commands%%'",
                    (bindings.db.role,),
                )
                assert len(rows) == 1
                bindings.db.query(f"KILL QUERY {int(rows[0]['pid'])}")
            assert pending.result(timeout=5).recovery == "query_original"
        recovered = bindings.port.recover(command)
    assert (
        recovered.draft == original.draft
        and len(bindings.snapshot()["speech_binding_revisions"]) == 1
    )


@pytest.mark.parametrize("size", [9007199254740993, 10**5000], ids=["above-safe", "large-integer"])
def test_large_definition_and_source_allowed_integer_survive_both_engines(
    bindings: Bindings,
    size: int,
) -> None:
    request = binding_request()
    artifact = request["artifact_set"][0]
    artifact["size_bytes"] = size
    request["artifact_set"] = [
        {**artifact, "artifact_id": "synthetic-" + str(i), "media_type": "x" * 128}
        for i in range(300 if size == 9007199254740993 else 15)
    ]
    command = prepare_binding_draft(request, "large-original-key")
    assert len(command.intent) > 65535
    created = bindings.port.create(command)
    assert created.draft is not None and created.draft.definition == command.definition
    wire = draft_to_wire(created.draft)
    assert wire["artifact_set"][0]["size_bytes"] == size
    assert bindings.port.get(command.binding_id, "1") == created.draft


def test_writer_cannot_rewrite_history_or_access_job_authority(bindings: Bindings) -> None:
    bindings.port.create(bindings.prepare())
    before = bindings.snapshot()
    for statement in (
        "UPDATE speech_binding_revisions SET definition_json='{}'",
        "UPDATE speech_binding_revisions SET revision_id='changed'",
        "UPDATE speech_binding_commands SET intent_json='{}'",
        "UPDATE speech_binding_commands SET request_digest='changed'",
        "UPDATE speech_binding_revisions SET key_digest='changed'",
        "DELETE FROM speech_binding_commands",
        "DELETE FROM speech_binding_revisions",
        "DELETE FROM speech_binding_families",
        "SELECT * FROM coordination_jobs",
        "SELECT * FROM coordination_outbox",
        "ALTER TABLE speech_binding_revisions ADD COLUMN forbidden BIGINT",
    ):
        with pytest.raises((psycopg.Error, pymysql.Error)), bindings.writer() as (_, cursor):
            cursor.execute(statement)
    assert bindings.snapshot() == before and bindings.db.counts() == (0, 0)


def test_runtime_insert_cannot_attach_a_second_revision_to_an_original_key(
    bindings: Bindings,
) -> None:
    assert bindings.port.create(bindings.prepare()).outcome == "created"
    before = bindings.snapshot()
    with (
        pytest.raises((psycopg.IntegrityError, pymysql.IntegrityError)),
        bindings.writer() as (_, cursor),
    ):
        cursor.execute(
            "INSERT INTO speech_binding_revisions "
            "(binding_id,revision,revision_id,key_digest,document_version,definition_json,"
            "definition_digest,request_digest,created_at_us) "
            "SELECT binding_id,'2','mbrev-forbidden-copy',key_digest,document_version,"
            "definition_json,definition_digest,request_digest,created_at_us "
            "FROM speech_binding_revisions"
        )
    assert bindings.snapshot() == before


def test_full_revision_range_and_exhaustion_preserve_existing_history(bindings: Bindings) -> None:
    command = bindings.prepare()
    bindings.db.query(
        "INSERT INTO speech_binding_families (binding_id,latest_revision) VALUES (%s,%s)",
        (command.binding_id, "9999999999999999998"),
    )
    created = bindings.port.create(command)
    assert created.draft is not None and created.draft.revision == "9999999999999999999"
    before = bindings.snapshot()
    exhausted = bindings.prepare("exhausted-key")
    assert (
        bindings.port.create(exhausted).recovery
        == bindings.port.recover(exhausted).recovery
        == "query_original"
    )
    assert bindings.port.get(command.binding_id, created.draft.revision) == created.draft
    assert bindings.snapshot() == before


def test_corrupt_deep_record_is_unavailable_and_other_families_can_continue(
    bindings: Bindings,
) -> None:
    command = bindings.prepare()
    bindings.port.create(command)
    raw = b"[" * 65535 + b"]" * 65535
    bindings.db.query(
        "UPDATE speech_binding_revisions SET definition_json=%s,definition_digest=%s",
        (raw.decode(), document_digest(raw)),
    )
    before = bindings.snapshot()
    assert bindings.port.recover(command).recovery == "query_original"
    with pytest.raises(BindingUnavailableError):
        bindings.port.get(command.binding_id, "1")
    assert bindings.snapshot() == before
    assert (
        bindings.port.create(bindings.prepare("healthy-key", binding_id="healthy-family")).outcome
        == "created"
    )


def test_valid_but_substituted_definition_cannot_be_read_under_the_original_identity(
    bindings: Bindings,
) -> None:
    command = bindings.prepare()
    assert bindings.port.create(command).outcome == "created"
    altered = decode_document(command.definition)
    altered["logical_model"] = "other-valid-model"
    raw = encode_document(altered)
    bindings.db.query(
        "UPDATE speech_binding_revisions SET definition_json=%s,definition_digest=%s",
        (raw.decode(), document_digest(raw)),
    )
    before = bindings.snapshot()
    with pytest.raises(BindingUnavailableError):
        bindings.port.get(command.binding_id, "1")
    assert bindings.port.recover(command).recovery == "query_original"
    assert bindings.snapshot() == before


def test_keyless_private_recovery_never_reuses_a_new_preparation_identity(
    bindings: Bindings,
) -> None:
    command = prepare_binding_draft(binding_request())
    created = bindings.port.create(command)
    assert created.draft is not None
    before = bindings.snapshot()
    assert bindings.port.recover(command).draft == created.draft
    other = prepare_binding_draft(binding_request())
    assert other.key_digest != command.key_digest
    assert bindings.port.recover(other).recovery == "query_original"
    assert bindings.snapshot() == before


def test_committed_orphan_journal_stays_query_only_unknown_without_allocation(
    bindings: Bindings,
) -> None:
    command = bindings.prepare()
    # Deliberate operator-fixture corruption; the production port commits the
    # journal and immutable result together and cannot produce this state.
    bindings.db.query(
        "INSERT INTO speech_binding_commands "
        "(key_digest,binding_id,document_version,intent_json,request_digest) "
        "VALUES (%s,%s,%s,%s,%s)",
        (
            command.key_digest,
            command.binding_id,
            1,
            command.intent.decode(),
            command.request_digest,
        ),
    )
    before = bindings.snapshot()
    assert (
        bindings.port.recover(command).recovery
        == bindings.port.create(command).recovery
        == "query_original"
    )
    assert bindings.snapshot() == before


def test_closed_pool_or_connector_preserves_unknown_and_never_reports_absence(
    bindings: Bindings,
) -> None:
    command = bindings.prepare()
    assert bindings.port.create(command).outcome == "created"
    before = bindings.snapshot()
    if bindings.mysql is None:
        assert bindings.db.pool is not None
        bindings.db.pool.close()
        assert (
            bindings.port.create(command).recovery
            == bindings.port.recover(command).recovery
            == "query_original"
        )
        with pytest.raises(BindingUnavailableError):
            bindings.port.get(command.binding_id, "1")
    else:

        def closed(_: Any) -> Any:
            raise pymysql.OperationalError(2003, "Synthetic closed test connector")

        with patch.object(MySQLJobTransactions, "connect", closed):
            assert (
                bindings.port.create(command).recovery
                == bindings.port.recover(command).recovery
                == "query_original"
            )
            with pytest.raises(BindingUnavailableError):
                bindings.port.get(command.binding_id, "1")
    assert bindings.snapshot() == before


def test_native_statement_deadline_does_not_create_a_second_revision(bindings: Bindings) -> None:
    command = bindings.prepare()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with bindings.writer() as (_, cursor):
            original = sql.create_draft(
                cursor, command, cursor.clock, lambda: "mbrev-before-deadline"
            )
            if bindings.mysql is None:
                short: BindingDrafts = PostgresBindingDrafts(
                    cast(SpeechPool, bindings.db.pool), timeout_seconds=0.2
                )
                pending = workers.submit(short.create, command)
                bindings.waiters(1)
                result = pending.result(timeout=3)
            else:
                connect = bindings.mysql.connector.connect

                def bounded() -> Any:
                    connection = connect()
                    connection.cursor().execute("SET SESSION innodb_lock_wait_timeout=1")
                    return connection

                with patch.object(MySQLJobTransactions, "connect", lambda _: bounded()):
                    pending = workers.submit(bindings.port.create, command)
                    bindings.waiters(1)
                    result = pending.result(timeout=3)
            assert result.recovery == "query_original"
        assert bindings.port.recover(command).draft == original.draft
    assert len(bindings.snapshot()["speech_binding_revisions"]) == 1


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_repeat_upgrade_and_refused_downgrade_preserve_binding_history(bindings: Bindings) -> None:
    bindings.port.create(bindings.prepare())
    before = bindings.snapshot()
    engine = create_engine(
        bindings.db.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = bindings.db.schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
            with pytest.raises(RuntimeError, match="Binding history downgrade"):
                migration.downgrade(config, "0009_profile_drafts")
    finally:
        engine.dispose()
    assert bindings.db.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0011_binding_parameter_defaults"}
    ]
    assert bindings.snapshot() == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_failed_0010_upgrade_rolls_back_new_ddl_and_preserves_old_rows_and_acl(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0009_profile_drafts")
    before = sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall()
    acl = sandbox.admin.execute(
        "SELECT relname,relacl FROM pg_class WHERE relnamespace=%s::regnamespace ORDER BY relname",
        (sandbox.schema,),
    ).fetchall()
    sandbox.admin.execute("CREATE TABLE speech_binding_commands (sentinel BIGINT)")
    with pytest.raises(ProgrammingError):
        sandbox.upgrade()
    assert sandbox.version() == "0009_profile_drafts"
    for name in ("speech_binding_families", "speech_binding_revisions"):
        assert (
            sandbox.admin.execute("SELECT to_regclass(%s) AS name", (name,)).fetchone()["name"]
            is None
        )
    assert sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall() == before
    assert (
        sandbox.admin.execute(
            "SELECT relname,relacl FROM pg_class WHERE relnamespace=%s::regnamespace "
            "AND relname<>'speech_binding_commands' ORDER BY relname",
            (sandbox.schema,),
        ).fetchall()
        == acl
    )
    sandbox.admin.execute("ALTER TABLE speech_binding_commands RENAME TO binding_collision_fixture")
    sandbox.upgrade()
    sandbox.upgrade()
    assert sandbox.version() == "0011_binding_parameter_defaults"
    assert sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall() == before
