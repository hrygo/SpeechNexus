"""Real immutable drafts, original-key recovery and CAS on PG and InnoDB."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event
from typing import Any, cast

import psycopg
import pymysql
import pytest
from alembic import command as migration
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine, event
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool
from tests.integration.test_coordination_foundation import UpgradeSandbox
from tests.integration.test_coordination_foundation import upgrade_sandbox as upgrade_sandbox
from tests.integration.test_production_jobs import ROOT, FaultConnection, JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.support.job_database import MySQLJobTransactions
from tests.support.migrations import HEAD
from tests.support.profile_database import ProfileCursor
from tests.support.pronunciation_database import MySQLPronunciationDrafts
from tests.support.pronunciation_examples import pronunciation_request

from speechnexus_backend.speech.application.pronunciation_drafts import PronunciationDrafts
from speechnexus_backend.speech.domain.pronunciation_drafts import (
    CreatePronunciationDraft,
    PronunciationUnavailableError,
    document_digest,
)
from speechnexus_backend.speech.infrastructure import pronunciation_drafts_sql as sql
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.pronunciation_draft_schema import (
    PRONUNCIATION_DRAFT_DDL,
    pronunciation_draft_grants,
)
from speechnexus_backend.speech.infrastructure.pronunciation_drafts_postgres import (
    PostgresPronunciationDrafts,
    _Cursor,
)
from speechnexus_backend.speech.interfaces.pronunciation_drafts import (
    draft_to_wire,
    prepare_pronunciation_draft,
)

pytestmark = pytest.mark.integration
TABLES = (
    "speech_pronunciation_commands",
    "speech_pronunciation_families",
    "speech_pronunciation_revisions",
)


@dataclass
class Pronunciations:
    db: JobDatabase
    port: PronunciationDrafts
    mysql: MySQLPronunciationDrafts | None = None

    def prepare(self, key: str = "original-key", **changes: Any) -> CreatePronunciationDraft:
        return prepare_pronunciation_draft(pronunciation_request(**changes), key)

    def snapshot(self) -> dict[str, Any]:
        return {table: self.db.query(f"SELECT * FROM {table} ORDER BY 1,2") for table in TABLES}

    @contextmanager
    def writer(self) -> Iterator[tuple[Any, Any]]:
        if self.mysql is not None:
            mysql_connection = self.mysql.connector.connect()
            try:
                with mysql_connection.cursor() as raw:
                    raw.execute("START TRANSACTION")
                    yield mysql_connection, ProfileCursor(raw)
            finally:
                mysql_connection.rollback()
                mysql_connection.close()
        else:
            assert self.db.pool is not None
            with self.db.pool.connection() as pg_connection, pg_connection.cursor() as pg_raw:
                yield pg_connection, _Cursor(pg_raw, time.monotonic() + 3)


@pytest.fixture
def pronunciations(database: JobDatabase) -> Pronunciations:
    db = database
    if db.dialect == "postgresql":
        db.query(f'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "{db.schema}" FROM "{db.role}"')
        for grant in pronunciation_draft_grants(db.schema, db.role):
            db.query(grant)
        assert db.pool is not None
        return Pronunciations(db, PostgresPronunciationDrafts(db.pool))
    for ddl in PRONUNCIATION_DRAFT_DDL:
        db.query(ddl.replace('COLLATE "C"', "COLLATE utf8mb4_0900_bin").replace("TEXT", "LONGTEXT"))
    db.query(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{db.role}'@'%'")
    for table in TABLES:
        db.query(f"GRANT SELECT, INSERT ON `{db.schema}`.{table} TO '{db.role}'@'%'")
    db.query(
        f"GRANT UPDATE (latest_revision) ON `{db.schema}`.speech_pronunciation_families "
        f"TO '{db.role}'@'%'"
    )
    db.query(
        f"GRANT UPDATE (command_state,pronunciation_id,revision,error_code) "
        f"ON `{db.schema}`.speech_pronunciation_commands TO '{db.role}'@'%'"
    )
    mysql = MySQLPronunciationDrafts(
        MySQLJobTransactions(db.url, db.schema, db.role, "synthetic_job_test")
    )
    return Pronunciations(db, mysql, mysql)


def test_auto_id_fixed_read_append_replay_and_real_typescript_consumer(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    command = p.prepare()
    first = p.port.create(command)
    assert first.outcome == "created" and first.draft is not None
    original = draft_to_wire(first.draft)
    assert original["status"] == "draft" and original["id"].startswith("pron-")
    created_id = first.draft.pronunciation_id
    second = p.port.create(
        p.prepare("append-key", id=created_id, expected_revision="1", encoding="ipa")
    )
    assert second.draft is not None and second.draft.revision == "2"
    before = p.snapshot()
    replay = p.port.create(p.prepare())
    assert replay.outcome == "replayed" and replay.draft == first.draft
    assert p.port.recover(command).draft == first.draft
    rebuilt: PronunciationDrafts = (
        MySQLPronunciationDrafts(p.mysql.connector)
        if p.mysql
        else PostgresPronunciationDrafts(cast(SpeechPool, p.db.pool))
    )
    assert rebuilt.get(created_id, "1") == first.draft
    assert rebuilt.get(created_id, "999") is None
    assert p.snapshot() == before and p.db.counts() == (0, 0)
    consumer = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            "import {parse} from './packages/contracts/src/index.ts'; "
            "let s='';for await (const c of process.stdin)s+=c;"
            "const v=parse('PronunciationRevision',JSON.parse(s));"
            "if(v.status!=='draft')throw Error('unexpected publication');"
            "process.stdout.write(JSON.stringify({id:v.id,revision:v.revision}));",
        ],
        cwd=ROOT,
        input=json.dumps(original),
        text=True,
        capture_output=True,
        check=False,
    )
    assert consumer.returncode == 0, consumer.stderr
    assert json.loads(consumer.stdout) == {"id": created_id, "revision": "1"}


def test_key_conflict_and_persisted_cas_rejection_do_not_allocate_later(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    first = p.port.create(p.prepare())
    assert first.draft is not None
    identity = first.draft.pronunciation_id
    before = p.snapshot()
    conflict = p.port.create(p.prepare(encoding="ipa"))
    assert conflict.outcome == "conflict" and p.snapshot() == before
    future = p.prepare("future-key", id=identity, expected_revision="2")
    assert p.port.create(future).error_code == "configuration_changed"
    next_revision = p.port.create(p.prepare("append-key", id=identity, expected_revision="1"))
    assert next_revision.draft is not None and next_revision.draft.revision == "2"
    after = p.snapshot()
    assert p.port.create(future).outcome == "rejected"
    assert p.port.recover(future).outcome == "rejected"
    assert p.snapshot() == after


@pytest.mark.parametrize("disposition", ["commit", "rollback", "different-intent"])
def test_real_unique_key_wait_recovers_original_or_rolled_back_writer(
    pronunciations: Pronunciations,
    disposition: str,
) -> None:
    p = pronunciations
    command = p.prepare()
    other = p.prepare(encoding="ipa") if disposition == "different-intent" else command
    with p.writer() as (connection, cursor):
        original = sql.create_draft(cursor, command, cursor.clock)
        assert original.draft is not None
        with ThreadPoolExecutor(max_workers=1) as workers:
            waiting = workers.submit(p.port.create, other)
            try:
                p.db.await_waiter()
            finally:
                connection.rollback() if disposition == "rollback" else connection.commit()
            result = waiting.result(timeout=4)
    assert (
        result.outcome
        == {"commit": "replayed", "rollback": "created", "different-intent": "conflict"}[
            disposition
        ]
    )
    rows = p.snapshot()
    assert all(len(rows[table]) == 1 for table in TABLES)
    assert p.db.counts() == (0, 0)
    if disposition == "commit":
        assert result.draft == original.draft


def test_real_family_lock_rechecks_cas_after_another_revision_commits(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    initial = p.port.create(p.prepare())
    assert initial.draft is not None
    changes = {"id": initial.draft.pronunciation_id, "expected_revision": "1"}
    with p.writer() as (connection, cursor):
        first = sql.create_draft(cursor, p.prepare("first-append", **changes), cursor.clock)
        with ThreadPoolExecutor(max_workers=1) as workers:
            waiting = workers.submit(p.port.create, p.prepare("second-append", **changes))
            try:
                p.db.await_waiter()
            finally:
                connection.commit()
            second = waiting.result(timeout=4)
    assert first.draft is not None and first.draft.revision == "2"
    assert second.outcome == "rejected" and second.error_code == "configuration_changed"
    assert len(p.snapshot()["speech_pronunciation_revisions"]) == 2


@pytest.mark.parametrize("stage", ["families", "commands"])
def test_failures_after_revision_write_roll_back_the_complete_command(
    pronunciations: Pronunciations,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    p = pronunciations
    original = sql._update

    def fail(cursor: Any, statement: str, args: tuple[object, ...]) -> None:
        if statement.startswith("UPDATE speech_pronunciation_" + stage):
            raise PronunciationUnavailableError
        original(cursor, statement, args)

    monkeypatch.setattr(sql, "_update", fail)
    command = p.prepare()
    assert p.port.create(command).recovery == "query_original"
    assert p.port.recover(command).outcome == "unknown"
    assert all(not rows for rows in p.snapshot().values())
    monkeypatch.setattr(sql, "_update", original)
    assert p.port.create(command).outcome == "created"


def test_unknown_recovery_and_corrupt_history_never_create_replacements(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    command = p.prepare()
    assert p.port.recover(command).outcome == "unknown"
    assert all(not rows for rows in p.snapshot().values())
    first = p.port.create(command)
    assert first.draft is not None
    raw = b"[" * 2000 + b"]" * 2000
    p.db.query(
        "UPDATE speech_pronunciation_revisions SET definition_json=%s,definition_digest=%s",
        (raw.decode(), document_digest(raw)),
    )
    before = p.snapshot()
    assert p.port.recover(command).outcome == "unknown"
    assert p.port.create(command).outcome == "unknown"
    with pytest.raises(PronunciationUnavailableError):
        p.port.get(first.draft.pronunciation_id, "1")
    assert p.snapshot() == before
    assert p.port.create(p.prepare("healthy-key")).outcome == "created"


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM speech_pronunciation_revisions",
        "UPDATE speech_pronunciation_revisions SET definition_json='{}'",
        "UPDATE speech_pronunciation_commands SET intent_json='{}'",
        "INSERT INTO coordination_jobs(job_id) VALUES ('forbidden-job')",
        "CREATE TABLE forbidden_pronunciation_ddl(id INTEGER)",
    ],
)
def test_minimum_writer_cannot_erase_or_rewrite_history_or_create_jobs(
    pronunciations: Pronunciations,
    statement: str,
) -> None:
    p = pronunciations
    assert p.port.create(p.prepare()).draft is not None
    before = p.snapshot()
    if p.mysql:
        with pytest.raises(pymysql.Error) as mysql_error, p.mysql.connector.transaction() as cursor:
            cursor.execute(statement)
        assert mysql_error.value.args[0] in (1044, 1142, 1143)
    else:
        assert p.db.pool is not None
        with pytest.raises(psycopg.Error) as pg_error, p.db.pool.connection() as connection:
            connection.execute(statement)
        assert pg_error.value.sqlstate == "42501"
    assert p.snapshot() == before and p.db.counts() == (0, 0)


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("fault", ["before", "after"])
def test_real_pg_commit_boundary_and_original_recovery(
    pronunciations: Pronunciations, fault: str
) -> None:
    p = pronunciations

    def configure(connection: FaultConnection) -> None:
        connection.execute(f'SET search_path TO "{p.db.schema}", pg_catalog')
        connection.commit()

    with ConnectionPool[FaultConnection](
        make_conninfo(p.db.url, user=p.db.role, password="synthetic_job_test"),
        connection_class=FaultConnection,
        kwargs={"row_factory": dict_row},
        configure=configure,
        min_size=1,
        max_size=1,
        timeout=3,
    ) as pool:
        with pool.connection() as connection:
            armed = connection
        armed.admin_url, armed.fault = p.db.url, fault
        port = PostgresPronunciationDrafts(cast(SpeechPool, pool))
        command = p.prepare()
        assert port.create(command).outcome == "unknown"
        before = p.snapshot()
        assert len(before["speech_pronunciation_revisions"]) == (fault == "after")
        assert port.recover(command).outcome == ("replayed" if fault == "after" else "unknown")
        assert p.snapshot() == before and p.db.counts() == (0, 0)
        if fault == "after":
            with pool.connection():
                pressured = PostgresPronunciationDrafts(
                    cast(SpeechPool, pool), timeout_seconds=0.05
                )
                assert pressured.recover(command).outcome == "unknown"
                assert p.snapshot() == before
            assert port.recover(command).outcome == "replayed"


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_closed_pool_keeps_original_unknown_and_fixed_read_unavailable(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    assert p.db.pool is not None
    command = p.prepare()
    initial = p.port.create(command)
    assert initial.draft is not None
    before = p.snapshot()
    p.db.pool.close()
    assert p.port.create(command).outcome == "unknown"
    assert p.port.recover(command).outcome == "unknown"
    with pytest.raises(PronunciationUnavailableError):
        p.port.get(initial.draft.pronunciation_id, "1")
    assert p.snapshot() == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_migration_is_repeatable_and_preserves_any_committed_pronunciation_history(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    s = upgrade_sandbox
    s.upgrade("0013_profile_lifecycle_intents")
    old = s.admin.execute("SELECT * FROM coordination_jobs").fetchall()
    s.upgrade()
    s.upgrade()
    assert s.version() == HEAD
    with s.engine.connect() as connection:
        s.config.attributes["connection"] = connection
        migration.downgrade(s.config, "0013_profile_lifecycle_intents")
    assert s.version() == "0013_profile_lifecycle_intents"
    s.upgrade()
    s.admin.execute(
        "INSERT INTO speech_pronunciation_families(pronunciation_id) VALUES ('pron-history')"
    )
    with s.engine.connect() as connection:
        s.config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="Pronunciation history"):
            migration.downgrade(s.config, "0013_profile_lifecycle_intents")
    assert s.version() == HEAD
    assert s.admin.execute("SELECT * FROM coordination_jobs").fetchall() == old
    assert s.admin.execute(
        "SELECT pronunciation_id FROM speech_pronunciation_families"
    ).fetchall() == [{"pronunciation_id": "pron-history"}]


def test_original_key_cannot_repoint_to_another_identical_auto_create(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    first_cmd, second_cmd = p.prepare(), p.prepare("second-original-key")
    assert first_cmd.intent == second_cmd.intent and first_cmd.key_digest != second_cmd.key_digest
    first, second = p.port.create(first_cmd), p.port.create(second_cmd)
    assert first.draft is not None and second.draft is not None
    assert first.draft.pronunciation_id != second.draft.pronunciation_id
    before = p.snapshot()
    with (
        pytest.raises((psycopg.IntegrityError, pymysql.IntegrityError)) as failure,
        p.writer() as (_, cursor),
    ):
        cursor.execute(
            "UPDATE speech_pronunciation_commands SET pronunciation_id=%s,revision=%s "
            "WHERE key_digest=%s",
            (second.draft.pronunciation_id, second.draft.revision, first_cmd.key_digest),
        )
    if p.mysql:
        assert failure.value.args[0] == 1452
    else:
        assert getattr(failure.value, "sqlstate", None) == "23503"
    assert p.snapshot() == before
    assert p.port.recover(first_cmd).draft == first.draft


def test_missing_parent_is_unavailable_and_cannot_be_fabricated_by_append(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    p.db.query(
        "INSERT INTO speech_pronunciation_families(pronunciation_id,latest_revision) "
        "VALUES ('pron-missing-parent','1')"
    )
    before = p.snapshot()
    cmd = p.prepare(id="pron-missing-parent", expected_revision="1")
    assert p.port.create(cmd).outcome == "unknown"
    assert p.port.recover(cmd).outcome == "unknown"
    assert p.snapshot() == before


def test_full_revision_range_uses_real_parent_and_never_wraps(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    first = p.port.create(p.prepare())
    assert first.draft is not None
    identity = first.draft.pronunciation_id
    # A synthetic immutable ancestor exercises the frozen 19-digit boundary;
    # it is not claimed as a real history containing 10**19 revisions.
    p.db.query(
        "INSERT INTO speech_pronunciation_revisions "
        "(pronunciation_id,revision,definition_json,definition_digest,key_digest,"
        "request_digest,created_at_us) "
        "SELECT pronunciation_id,'9999999999999999998',definition_json,definition_digest,%s,"
        "request_digest,created_at_us FROM speech_pronunciation_revisions "
        "WHERE pronunciation_id=%s AND revision='1'",
        ("sha256:" + "d" * 64, identity),
    )
    p.db.query(
        "UPDATE speech_pronunciation_families SET latest_revision='9999999999999999998' "
        "WHERE pronunciation_id=%s",
        (identity,),
    )
    cmd = p.prepare("last-revision-key", id=identity, expected_revision="9999999999999999998")
    maximum = p.port.create(cmd)
    assert maximum.draft is not None and maximum.draft.revision == "9999999999999999999"
    assert p.port.recover(cmd).draft == maximum.draft
    before = p.snapshot()
    exhausted = p.prepare(
        "exhausted-revision-key", id=identity, expected_revision=maximum.draft.revision
    )
    assert p.port.create(exhausted).outcome == "unknown"
    assert p.port.recover(exhausted).outcome == "unknown"
    assert p.snapshot() == before


def test_declared_max_entries_survive_actual_database_text_round_trip(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    cmd = p.prepare(entries=[{"grapheme": "synthetic", "phonemes": "synthetic"}] * 10000)
    assert len(cmd.intent) > 262144
    result = p.port.create(cmd)
    assert result.draft is not None
    assert p.port.get(result.draft.pronunciation_id, "1") == result.draft
    assert len(draft_to_wire(result.draft)["entries"]) == 10000


@pytest.mark.parametrize("database", ["mysql"], indirect=True)
def test_innodb_lost_commit_reply_uses_original_committed_result(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    assert p.mysql is not None
    cmd = p.prepare()
    p.mysql.lose_reply = True
    assert p.port.create(cmd).outcome == "unknown"
    before = p.snapshot()
    assert len(before["speech_pronunciation_revisions"]) == 1
    assert p.port.recover(cmd).outcome == "replayed"
    assert p.port.create(cmd).outcome == "replayed"
    assert p.snapshot() == before


def downgrade_pronunciations(
    db: JobDatabase,
    *,
    application: str,
    after_checks: Callable[[], None] | None = None,
    isolation: str = "READ COMMITTED",
) -> None:
    engine = create_engine(
        db.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"application_name": application},
        isolation_level=isolation,
    )

    def pause(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if after_checks is not None and statement.startswith(
            "SELECT count(*) FROM speech_pronunciation_revisions"
        ):
            after_checks()

    event.listen(engine, "after_cursor_execute", pause)
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = db.schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.downgrade(config, "0013_profile_lifecycle_intents")
    finally:
        engine.dispose()


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_writer_commit_while_downgrade_waits_preserves_all_new_history(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    cmd = p.prepare()
    application = "sn_pron_down_" + p.db.schema
    with ThreadPoolExecutor(max_workers=1) as workers:
        with p.writer() as (connection, cursor):
            original = sql.create_draft(cursor, cmd, cursor.clock)
            pending = workers.submit(downgrade_pronunciations, p.db, application=application)
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    rows = p.db.query(
                        "SELECT pid FROM pg_stat_activity WHERE application_name=%s "
                        "AND cardinality(pg_blocking_pids(pid))>0",
                        (application,),
                    )
                    if rows:
                        break
                    assert not pending.done(), "Migration must wait for the active writer"
                    time.sleep(0.01)
                else:
                    pytest.fail("Actual pronunciation migration lock wait was not observed")
            finally:
                connection.commit()
        with pytest.raises(RuntimeError, match="Pronunciation history"):
            pending.result(timeout=5)
    assert p.port.recover(cmd).draft == original.draft
    assert all(len(rows) == 1 for rows in p.snapshot().values())
    assert p.db.query("SELECT version_num FROM alembic_version") == [{"version_num": HEAD}]


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_downgrade_locks_before_checking_so_waiting_writer_never_succeeds_then_loses_history(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    at_counts, release = Event(), Event()

    def pause() -> None:
        at_counts.set()
        assert release.wait(6), "Actual downgrade transaction was not released"

    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(
            downgrade_pronunciations,
            p.db,
            application="sn_pron_first_" + p.db.schema,
            after_checks=pause,
        )
        try:
            assert at_counts.wait(5)
            writer = workers.submit(p.port.create, p.prepare())
            p.db.await_waiter()
            assert not writer.done(), "A writer must not succeed between emptiness checks and DROP"
        finally:
            release.set()
        pending.result(timeout=5)
        assert writer.result(timeout=5).outcome == "unknown"
    assert p.db.query("SELECT to_regclass('speech_pronunciation_commands') AS t") == [{"t": None}]
    assert p.db.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0013_profile_lifecycle_intents"}
    ]


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_downgrade_rejects_stale_transaction_snapshots_before_ddl(
    pronunciations: Pronunciations, isolation: str
) -> None:
    p = pronunciations
    before = p.snapshot()
    with pytest.raises(RuntimeError, match="READ COMMITTED"):
        downgrade_pronunciations(p.db, application="sn_pron_isolation", isolation=isolation)
    assert p.snapshot() == before


def test_native_cancel_of_original_read_does_not_mean_missing_or_permit_replacement(
    pronunciations: Pronunciations,
) -> None:
    p = pronunciations
    command = p.prepare()
    created = p.port.create(command)
    assert created.draft is not None
    before = p.snapshot()
    blocker = p.db.blocker()
    try:
        blocker.cursor().execute(
            "LOCK TABLES speech_pronunciation_commands WRITE"
            if p.mysql
            else "LOCK TABLE speech_pronunciation_commands IN ACCESS EXCLUSIVE MODE"
        )
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(p.port.recover, command)
            if p.mysql:
                deadline, rows = time.monotonic() + 3, []
                while time.monotonic() < deadline:
                    rows = p.db.query(
                        "SELECT ID AS pid FROM information_schema.PROCESSLIST WHERE USER=%s "
                        "AND STATE='Waiting for table metadata lock'",
                        (p.db.role,),
                    )
                    if rows:
                        break
                    time.sleep(0.01)
                assert len(rows) == 1
                p.db.query(f"KILL QUERY {int(rows[0]['pid'])}")
            else:
                p.db.await_waiter()
                rows = p.db.query(
                    "SELECT pid FROM pg_stat_activity WHERE usename=%s "
                    "AND cardinality(pg_blocking_pids(pid))>0",
                    (p.db.role,),
                )
                assert len(rows) == 1
                assert p.db.query("SELECT pg_cancel_backend(%s) AS cancelled", (rows[0]["pid"],))[
                    0
                ]["cancelled"]
            assert pending.result(timeout=5).outcome == "unknown"
    finally:
        if p.mysql:
            blocker.cursor().execute("UNLOCK TABLES")
        blocker.rollback()
        blocker.close()
    assert p.port.recover(command).draft == created.draft
    assert p.snapshot() == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_failed_new_migration_rolls_back_all_ddl_without_losing_previous_history(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    s = upgrade_sandbox
    s.upgrade("0013_profile_lifecycle_intents")
    old = s.admin.execute("SELECT * FROM coordination_jobs").fetchall()
    s.admin.execute("CREATE TABLE speech_pronunciation_commands(sentinel BIGINT)")
    with pytest.raises(ProgrammingError):
        s.upgrade()
    assert s.version() == "0013_profile_lifecycle_intents"
    for table in ("speech_pronunciation_families", "speech_pronunciation_revisions"):
        assert s.admin.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"] is None
    assert s.admin.execute("SELECT * FROM coordination_jobs").fetchall() == old
    s.admin.execute("ALTER TABLE speech_pronunciation_commands RENAME TO pron_collision_fixture")
    s.upgrade()
    assert s.version() == HEAD
