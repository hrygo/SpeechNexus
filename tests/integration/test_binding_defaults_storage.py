"""Real PG authority / InnoDB transaction probes; no engine or production operations."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from typing import Any, cast
from unittest.mock import patch

import psycopg
import pymysql
import pytest
from alembic import command as migration
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine, event
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool
from tests.integration.test_binding_drafts import Bindings
from tests.integration.test_binding_drafts import bindings as bindings
from tests.integration.test_coordination_foundation import UpgradeSandbox
from tests.integration.test_coordination_foundation import upgrade_sandbox as upgrade_sandbox
from tests.integration.test_production_jobs import ROOT, FaultConnection, JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.support.binding_defaults_database import MySQLBindingDefaults
from tests.support.binding_examples import binding_request
from tests.support.job_database import MySQLJobTransactions

from speechnexus_backend.speech.application.binding_default_drafts import BindingDefaultDrafts
from speechnexus_backend.speech.domain.binding_drafts import (
    BindingUnavailableError,
    CreateBindingDraft,
)
from speechnexus_backend.speech.infrastructure import binding_defaults_sql as sql
from speechnexus_backend.speech.infrastructure.binding_defaults_postgres import (
    PostgresBindingDefaults,
)
from speechnexus_backend.speech.infrastructure.binding_defaults_schema import (
    BINDING_DEFAULTS_DDL,
    PARENT_KEY,
    TABLE,
    binding_defaults_grants,
)
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import _Cursor
from speechnexus_backend.speech.infrastructure.binding_drafts_sql import create_draft
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.interfaces.binding_default_drafts import (
    defaults_to_wire,
    prepare_binding_defaults_draft,
)

pytestmark = pytest.mark.integration


def downgrade_defaults(
    db: JobDatabase,
    *,
    application: str,
    before_counts: Callable[[], None] | None = None,
    isolation: str = "READ COMMITTED",
) -> None:
    from alembic.config import Config

    engine = create_engine(
        db.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"application_name": application},
        isolation_level=isolation,
    )

    def before_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if before_counts is not None and statement.startswith(f"SELECT count(*) FROM {TABLE}"):
            before_counts()

    event.listen(engine, "before_cursor_execute", before_execute)
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = db.schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.downgrade(config, "0010_model_binding_drafts")
    finally:
        engine.dispose()


@dataclass
class Defaults:
    bindings: Bindings
    port: BindingDefaultDrafts
    mysql: MySQLBindingDefaults | None = None

    def prepare(self, key: str | None = "original-key", **changes: Any) -> CreateBindingDraft:
        return prepare_binding_defaults_draft(
            {
                **binding_request(),
                "parameter_defaults": {"asr": {"max_segment_ms": 15000}},
                **changes,
            },
            key,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.bindings.snapshot(),
            TABLE: self.bindings.db.query(f"SELECT * FROM {TABLE} ORDER BY snapshot_id"),
        }


@pytest.fixture
def defaults(bindings: Bindings) -> Defaults:
    db = bindings.db
    if bindings.mysql is None:
        for grant in binding_defaults_grants(db.schema, db.role):
            db.query(grant)
        return Defaults(bindings, PostgresBindingDefaults(cast(SpeechPool, db.pool)))
    for ddl in BINDING_DEFAULTS_DDL:
        db.query(ddl.replace('COLLATE "C"', "COLLATE utf8mb4_0900_bin").replace("TEXT", "LONGTEXT"))
    db.query(f"GRANT SELECT, INSERT ON `{db.schema}`.{TABLE} TO '{db.role}'@'%'")
    mysql = MySQLBindingDefaults(bindings.mysql.connector)
    return Defaults(bindings, mysql, mysql)


@pytest.mark.parametrize("parameters", [{}, {"asr": {"max_segment_ms": 15000}}])
def test_atomic_parent_snapshot_original_replay_restart_and_fixed_projection(
    defaults: Defaults, parameters: dict[str, Any]
) -> None:
    cmd = defaults.prepare(parameter_defaults=parameters)
    first = defaults.port.create(cmd)
    assert first.outcome == "created" and first.draft is not None and first.snapshot is not None
    assert defaults_to_wire(first.snapshot)["parameters"] == parameters
    consumed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            'import {parse} from "./packages/contracts/src/binding_defaults.ts";'
            'import {readFileSync} from "node:fs";'
            'const value=JSON.parse(readFileSync(0,"utf8"));'
            'const result=parse("BindingParameterDefaultsSnapshot",value);'
            "if(JSON.stringify(result)!==JSON.stringify(value))process.exit(1);"
            "process.stdout.write(JSON.stringify(result));",
        ],
        input=json.dumps(defaults_to_wire(first.snapshot)),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=10,
        check=False,
    )
    assert consumed.returncode == 0, consumed.stderr
    assert json.loads(consumed.stdout) == defaults_to_wire(first.snapshot)
    assert first.snapshot.revision_id == first.draft.revision_id
    assert first.snapshot.created_at_us == first.draft.created_at_us
    before = defaults.snapshot()
    assert len(before[TABLE]) == len(before["speech_binding_revisions"]) == 1
    assert bool(before["speech_binding_commands"][0]["defaults_declared"])
    assert before["speech_binding_commands"][0]["intent_json"].encode() == cmd.intent
    assert defaults.port.create(cmd).snapshot == first.snapshot
    rebuilt = (
        MySQLBindingDefaults(defaults.mysql.driver.connector)
        if defaults.mysql is not None
        else PostgresBindingDefaults(cast(SpeechPool, defaults.bindings.db.pool))
    )
    assert rebuilt.recover(cmd).snapshot == first.snapshot
    assert rebuilt.get_defaults(cmd.binding_id, "1") == first.snapshot
    assert rebuilt.get(cmd.binding_id, "1") == first.draft
    assert rebuilt.get_defaults(cmd.binding_id, "99") is None
    assert defaults.snapshot() == before and defaults.bindings.db.counts() == (0, 0)


def test_omitted_and_legacy_rows_never_gain_default_facts(defaults: Defaults) -> None:
    old = defaults.bindings.prepare("original-old-key")
    first = defaults.bindings.port.create(old)
    assert first.outcome == "created"
    assert defaults.port.get_defaults(old.binding_id, "1") is None
    omitted = prepare_binding_defaults_draft(binding_request(), "new-omitted-key")
    second = defaults.port.create(omitted)
    assert second.outcome == "created" and second.draft is not None and second.snapshot is None
    assert defaults.port.get_defaults(old.binding_id, "2") is None
    before = defaults.snapshot()
    assert defaults.port.recover(old).draft == first.draft
    assert defaults.port.create(old).snapshot is None
    assert defaults.port.recover(omitted).snapshot is None
    assert defaults.snapshot() == before and before[TABLE] == []
    assert [r["defaults_declared"] for r in before["speech_binding_commands"]].count(None) == 1
    if defaults.mysql is None:
        with pytest.raises(ValueError, match="atomic snapshot"):
            defaults.bindings.port.create(defaults.prepare("not-accepted"))
        assert defaults.snapshot() == before


@pytest.mark.parametrize("original", [False, True])
def test_omission_empty_and_changed_values_conflict_under_original_key(
    defaults: Defaults, original: bool
) -> None:
    omitted = prepare_binding_defaults_draft(binding_request(), "original-key")
    explicit = defaults.prepare(parameter_defaults={})
    first, second = (explicit, omitted) if original else (omitted, explicit)
    assert defaults.port.create(first).outcome == "created"
    before = defaults.snapshot()
    for changed in (second, defaults.prepare(), defaults.prepare(binding_id="other-family")):
        for result in (defaults.port.create(changed), defaults.port.recover(changed)):
            assert (
                result.outcome == "conflict" and result.binding.error_code == "idempotency_conflict"
            )
        assert defaults.snapshot() == before


@pytest.mark.parametrize("same_intent", [False, True])
def test_same_key_native_wait_replays_or_conflicts_without_second_snapshot(
    defaults: Defaults, same_intent: bool
) -> None:
    cmd = defaults.prepare()
    rival = cmd if same_intent else defaults.prepare(parameter_defaults={})
    with ThreadPoolExecutor(max_workers=1) as workers:
        with defaults.bindings.writer() as (_, cursor):
            first = sql.create_default_draft(
                cursor, cmd, cursor.clock, lambda: "mbrev-first", lambda: "mbdefaults-first"
            )
            pending = workers.submit(defaults.port.create, rival)
            defaults.bindings.waiters(1)
            assert not pending.done()
        result = pending.result(timeout=5)
    assert result.outcome == ("replayed" if same_intent else "conflict")
    assert result.snapshot == (first.snapshot if same_intent else None)
    assert len(defaults.snapshot()[TABLE]) == 1


def test_rolled_back_original_allows_waiting_original_to_create_once(defaults: Defaults) -> None:
    cmd = defaults.prepare()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with defaults.bindings.writer() as (connection, cursor):
            discarded = sql.create_default_draft(
                cursor,
                cmd,
                cursor.clock,
                lambda: "mbrev-rolled-back",
                lambda: "mbdefaults-rolled-back",
            )
            pending = workers.submit(defaults.port.create, cmd)
            defaults.bindings.waiters(1)
            connection.rollback()
        result = pending.result(timeout=5)
    assert result.outcome == "created" and result.snapshot is not None
    assert discarded.snapshot is not None and result.snapshot != discarded.snapshot
    assert defaults.port.recover(cmd).snapshot == result.snapshot
    assert len(defaults.snapshot()[TABLE]) == 1


def test_different_keys_append_distinct_snapshots_to_one_family(defaults: Defaults) -> None:
    first = defaults.port.create(defaults.prepare())
    assert first.outcome == "created"
    cmds = [defaults.prepare("original-key-" + str(i), parameter_defaults={}) for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(defaults.port.create, cmds))
    assert all(r.outcome == "created" for r in results)
    assert {r.draft.revision for r in results if r.draft} == {"2", "3"}
    assert len({r.snapshot.snapshot_id for r in results if r.snapshot}) == 2
    assert len(defaults.snapshot()[TABLE]) == 3


@pytest.mark.parametrize("stage", ["before-child", "after-child"])
def test_child_failure_rolls_back_original_parent_and_family_head(
    defaults: Defaults, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    assert defaults.port.create(defaults.prepare()).outcome == "created"
    before = defaults.snapshot()
    original = sql.read_default_snapshot
    if stage == "before-child":

        def fail() -> str:
            raise BindingUnavailableError

        if defaults.mysql:
            defaults.mysql.snapshot_identity = fail
        else:
            monkeypatch.setattr(defaults.port, "_snapshot_identity", fail)
    else:

        def fail_read(*args: Any) -> Any:
            original(*args)
            raise BindingUnavailableError

        monkeypatch.setattr(sql, "read_default_snapshot", fail_read)
    cmd = defaults.prepare("failed-new-key")
    assert defaults.port.create(cmd).recovery == "query_original"
    assert defaults.snapshot() == before
    assert defaults.port.recover(cmd).outcome == "unknown"


def test_snapshot_identity_collision_rolls_back_new_parent_and_original(defaults: Defaults) -> None:
    if defaults.mysql:
        defaults.mysql.snapshot_identity = lambda: "mbdefaults-collision"
    else:
        defaults.port = PostgresBindingDefaults(
            cast(SpeechPool, defaults.bindings.db.pool),
            snapshot_identity_factory=lambda: "mbdefaults-collision",
        )
    assert defaults.port.create(defaults.prepare()).outcome == "created"
    before = defaults.snapshot()
    cmd = defaults.prepare("other-key", binding_id="other-family")
    assert defaults.port.create(cmd).recovery == "query_original"
    assert defaults.snapshot() == before and defaults.port.recover(cmd).outcome == "unknown"


def test_actual_commit_loss_then_read_pressure_recovers_exact_original(defaults: Defaults) -> None:
    cmd = defaults.prepare()
    db = defaults.bindings.db
    if defaults.mysql:
        defaults.mysql.driver.lose_reply = True
        result = defaults.port.create(cmd)
    else:

        def configure(connection: FaultConnection) -> None:
            connection.execute(f'SET search_path TO "{db.schema}",pg_catalog')
            connection.commit()

        with ConnectionPool[FaultConnection](
            make_conninfo(db.url, user=db.role, password="synthetic_job_test"),
            connection_class=FaultConnection,
            kwargs={"row_factory": dict_row},
            configure=configure,
            min_size=1,
            max_size=1,
        ) as fault_pool:
            with fault_pool.connection() as connection:
                armed = connection
            armed.fault = "after"
            result = PostgresBindingDefaults(cast(SpeechPool, fault_pool)).create(cmd)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    before = defaults.snapshot()
    assert len(before[TABLE]) == len(before["speech_binding_revisions"]) == 1
    if defaults.mysql:
        blocker = db.blocker()
        try:
            blocker.cursor().execute(f"LOCK TABLES {TABLE} WRITE")
            connect = defaults.mysql.driver.connector.connect

            def pressured() -> Any:
                connection = connect()
                connection.cursor().execute("SET SESSION lock_wait_timeout=1")
                return connection

            with patch.object(MySQLJobTransactions, "connect", lambda _: pressured()):
                assert defaults.port.recover(cmd).recovery == "query_original"
                with pytest.raises(BindingUnavailableError):
                    defaults.port.get_defaults(cmd.binding_id, "1")
        finally:
            blocker.cursor().execute("UNLOCK TABLES")
            blocker.close()
    else:
        with (
            speech_pool(
                make_conninfo(db.url, user=db.role, password="synthetic_job_test"),
                schema=db.schema,
                max_size=1,
            ) as pressure_pool,
            pressure_pool.connection(),
        ):
            pressure = PostgresBindingDefaults(pressure_pool, timeout_seconds=0.05)
            assert pressure.recover(cmd).recovery == "query_original"
            with pytest.raises(BindingUnavailableError):
                pressure.get_defaults(cmd.binding_id, "1")
    assert defaults.snapshot() == before
    restored = defaults.port.recover(cmd)
    assert restored.outcome == "replayed" and restored.snapshot is not None
    assert restored.snapshot.snapshot_id == before[TABLE][0]["snapshot_id"]
    assert defaults.snapshot() == before and db.counts() == (0, 0)


@pytest.mark.parametrize("damage", ["missing", "marker", "parameters", "digest", "identity"])
def test_damaged_declared_snapshot_is_unavailable_never_empty_or_replaced(
    defaults: Defaults, damage: str
) -> None:
    cmd = defaults.prepare()
    assert defaults.port.create(cmd).outcome == "created"
    db = defaults.bindings.db
    if damage == "missing":
        db.query(f"DELETE FROM {TABLE}")
    elif damage == "marker":
        db.query("UPDATE speech_binding_commands SET defaults_declared=NULL")
    else:
        field, value = {
            "parameters": ("parameters_json", "{}"),
            "digest": ("parameters_digest", "sha256:" + "0" * 64),
            "identity": ("snapshot_id", "mbdefaults-damaged"),
        }[damage]
        db.query(f"UPDATE {TABLE} SET {field}=%s", (value,))
    before = defaults.snapshot()
    with pytest.raises(BindingUnavailableError):
        defaults.port.get_defaults(cmd.binding_id, "1")
    for result in (defaults.port.create(cmd), defaults.port.recover(cmd)):
        assert result.recovery == "query_original" and result.snapshot is None
    assert defaults.snapshot() == before


def test_compound_parent_foreign_key_and_minimum_role(defaults: Defaults) -> None:
    assert defaults.port.create(defaults.prepare()).outcome == "created"
    db = defaults.bindings.db
    for field, value in (("created_at_us", 0), ("request_digest", "sha256:" + "0" * 64)):
        with pytest.raises((psycopg.IntegrityError, pymysql.IntegrityError)):
            db.query(f"UPDATE {TABLE} SET {field}=%s", (value,))
    before = defaults.snapshot()
    for statement in (
        f"UPDATE {TABLE} SET parameters_json='{{}}'",
        f"DELETE FROM {TABLE}",
        "SELECT * FROM coordination_jobs",
    ):
        with (
            pytest.raises((psycopg.errors.InsufficientPrivilege, pymysql.OperationalError)),
            defaults.bindings.writer() as (_, cursor),
        ):
            cursor.execute(statement)
    assert defaults.snapshot() == before


def test_cancelled_native_snapshot_read_preserves_original_and_no_allocation(
    defaults: Defaults,
) -> None:
    cmd = defaults.prepare()
    original = defaults.port.create(cmd)
    assert original.snapshot is not None
    before = defaults.snapshot()
    db = defaults.bindings.db
    blocker = db.blocker()
    try:
        blocker.cursor().execute(
            f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE"
            if db.dialect == "postgresql"
            else f"LOCK TABLES {TABLE} WRITE"
        )
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(defaults.port.recover, cmd)
            if db.dialect == "postgresql":
                db.await_waiter()
                rows = db.query(
                    "SELECT pid FROM pg_stat_activity WHERE usename=%s "
                    "AND cardinality(pg_blocking_pids(pid))>0",
                    (db.role,),
                )
                assert len(rows) == 1
                assert db.query("SELECT pg_cancel_backend(%s) AS cancelled", (rows[0]["pid"],))[0][
                    "cancelled"
                ]
            else:
                deadline = time.monotonic() + 2
                rows = []
                while time.monotonic() < deadline:
                    rows = db.query(
                        "SELECT ID AS pid FROM information_schema.PROCESSLIST WHERE USER=%s "
                        "AND STATE='Waiting for table metadata lock'",
                        (db.role,),
                    )
                    if rows:
                        break
                    time.sleep(0.01)
                assert len(rows) == 1
                db.query(f"KILL QUERY {int(rows[0]['pid'])}")
            assert pending.result(timeout=5).recovery == "query_original"
    finally:
        if db.dialect == "mysql":
            blocker.cursor().execute("UNLOCK TABLES")
        blocker.rollback()
        blocker.close()
    assert defaults.port.recover(cmd).snapshot == original.snapshot
    assert defaults.snapshot() == before


def test_keyless_prepared_identity_is_fixed_but_new_call_is_new_intent(defaults: Defaults) -> None:
    original = defaults.prepare(None)
    first = defaults.port.create(original)
    assert first.snapshot is not None
    assert defaults.port.recover(original).snapshot == first.snapshot
    another = defaults.prepare(None)
    assert another.key_digest != original.key_digest
    second = defaults.port.create(another)
    assert second.snapshot is not None and second.snapshot != first.snapshot
    assert len(defaults.snapshot()[TABLE]) == 2


def test_old_writer_role_cannot_read_or_create_default_snapshots(defaults: Defaults) -> None:
    cmd = defaults.prepare()
    original = defaults.port.create(cmd)
    assert original.snapshot is not None
    before = defaults.snapshot()
    db = defaults.bindings.db
    db.query(
        f'REVOKE ALL PRIVILEGES ON "{db.schema}".{TABLE} FROM "{db.role}"'
        if db.dialect == "postgresql"
        else f"REVOKE ALL PRIVILEGES ON `{db.schema}`.{TABLE} FROM '{db.role}'@'%'"
    )
    assert defaults.bindings.port.get(cmd.binding_id, "1") == original.draft
    assert defaults.port.recover(cmd).recovery == "query_original"
    with pytest.raises(BindingUnavailableError):
        defaults.port.get_defaults(cmd.binding_id, "1")
    assert defaults.port.create(defaults.prepare("denied-new-key")).recovery == "query_original"
    assert defaults.snapshot() == before


def test_closed_storage_access_is_unknown_and_rebuild_reads_original(defaults: Defaults) -> None:
    cmd = defaults.prepare()
    first = defaults.port.create(cmd)
    before = defaults.snapshot()
    if defaults.mysql:
        with patch.object(
            MySQLJobTransactions,
            "connect",
            side_effect=pymysql.OperationalError(2003, "synthetic unavailable"),
        ):
            assert defaults.port.recover(cmd).recovery == "query_original"
            with pytest.raises(BindingUnavailableError):
                defaults.port.get_defaults(cmd.binding_id, "1")
        assert defaults.port.recover(cmd).snapshot == first.snapshot
    else:
        db = defaults.bindings.db
        with speech_pool(
            make_conninfo(db.url, user=db.role, password="synthetic_job_test"),
            schema=db.schema,
        ) as pool:
            closed = PostgresBindingDefaults(pool)
        assert closed.recover(cmd).recovery == "query_original"
        with pytest.raises(BindingUnavailableError):
            closed.get_defaults(cmd.binding_id, "1")
        assert defaults.port.recover(cmd).snapshot == first.snapshot
    assert defaults.snapshot() == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_committed_reply_after_cooperative_deadline_recovers_original(defaults: Defaults) -> None:
    class DelayedCommit(psycopg.Connection[dict[str, Any]]):
        armed = False

        def commit(self) -> None:
            super().commit()
            if self.armed:
                self.armed = False
                time.sleep(0.6)

    db = defaults.bindings.db

    def configure(connection: DelayedCommit) -> None:
        connection.execute(f'SET search_path TO "{db.schema}",pg_catalog')
        connection.commit()

    cmd = defaults.prepare()
    with ConnectionPool[DelayedCommit](
        make_conninfo(db.url, user=db.role, password="synthetic_job_test"),
        connection_class=DelayedCommit,
        kwargs={"row_factory": dict_row},
        configure=configure,
        min_size=1,
        max_size=1,
    ) as pool:
        with pool.connection() as connection:
            armed = connection
        armed.armed = True
        result = PostgresBindingDefaults(cast(SpeechPool, pool), timeout_seconds=0.5).create(cmd)
    assert result.recovery == "query_original"
    before = defaults.snapshot()
    assert len(before[TABLE]) == 1
    recovered = defaults.port.recover(cmd)
    assert recovered.snapshot is not None
    assert recovered.snapshot.snapshot_id == before[TABLE][0]["snapshot_id"]
    assert defaults.snapshot() == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_empty_new_facts_roundtrip_and_history_preservation(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0010_model_binding_drafts")
    cmd = prepare_binding_defaults_draft(binding_request(), "old-history-key")
    with sandbox.admin.transaction(), sandbox.admin.cursor() as raw:
        cursor = _Cursor(raw, time.monotonic() + 3)
        original = create_draft(cursor, cmd, cursor.clock, lambda: "mbrev-old-history")
    assert original.outcome == "created"
    old_binding = sandbox.admin.execute("SELECT * FROM speech_binding_revisions").fetchall()
    prior = sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall()
    sandbox.upgrade("0011_binding_parameter_defaults")
    sandbox.upgrade("0011_binding_parameter_defaults")
    with sandbox.engine.connect() as connection:
        sandbox.config.attributes["connection"] = connection
        migration.downgrade(sandbox.config, "0010_model_binding_drafts")
    assert sandbox.version() == "0010_model_binding_drafts"
    assert sandbox.admin.execute(f"SELECT to_regclass('{TABLE}') AS t").fetchone()["t"] is None
    sandbox.upgrade("0011_binding_parameter_defaults")
    assert sandbox.version() == "0011_binding_parameter_defaults"
    assert sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall() == prior
    assert sandbox.admin.execute("SELECT * FROM speech_binding_revisions").fetchall() == old_binding
    assert sandbox.admin.execute(f"SELECT * FROM {TABLE}").fetchall() == []
    assert sandbox.admin.execute(
        "SELECT defaults_declared FROM speech_binding_commands"
    ).fetchall() == [{"defaults_declared": None}]


@pytest.mark.parametrize(
    "database", [("postgresql", "0011_binding_parameter_defaults")], indirect=True
)
@pytest.mark.parametrize("remove_child", [False, True])
def test_declared_history_blocks_downgrade_even_if_child_missing(
    defaults: Defaults, remove_child: bool
) -> None:
    assert defaults.port.create(defaults.prepare()).outcome == "created"
    db = defaults.bindings.db
    if remove_child:
        db.query(f"DELETE FROM {TABLE}")
    before = defaults.snapshot()
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine(
        db.url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = db.schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="default history"):
                migration.downgrade(config, "0010_model_binding_drafts")
    finally:
        engine.dispose()
    assert db.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0011_binding_parameter_defaults"}
    ]
    assert defaults.snapshot() == before


@pytest.mark.parametrize(
    "database", [("postgresql", "0011_binding_parameter_defaults")], indirect=True
)
def test_writer_commit_while_downgrade_waits_preserves_all_declared_facts(
    defaults: Defaults,
) -> None:
    db = defaults.bindings.db
    cmd = defaults.prepare()
    application = "sn_defaults_downgrade_" + db.schema
    with ThreadPoolExecutor(max_workers=1) as workers:
        with defaults.bindings.writer() as (_, cursor):
            accepted = sql.create_default_draft(
                cursor,
                cmd,
                cursor.clock,
                lambda: "mbrev-before-downgrade",
                lambda: "mbdefaults-before-downgrade",
            )
            # Snapshot this actual uncommitted transaction, not a later race with DROP.
            before = {}
            for table in (*defaults.bindings.snapshot().keys(), TABLE):
                cursor.execute(f"SELECT * FROM {table} ORDER BY 1,2")
                before[table] = cursor.fetchall()
            pending = workers.submit(downgrade_defaults, db, application=application)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = db.query(
                    "SELECT pid FROM pg_stat_activity WHERE application_name=%s "
                    "AND cardinality(pg_blocking_pids(pid))>0",
                    (application,),
                )
                if rows:
                    break
                assert not pending.done(), "Migration must wait for the active writer"
                time.sleep(0.01)
            else:
                pytest.fail("Actual downgrade lock wait was not observed")
        # The writer has really committed; the migration must now see and preserve it.
        with pytest.raises(RuntimeError, match="default history"):
            pending.result(timeout=5)
    assert defaults.port.recover(cmd).snapshot == accepted.snapshot
    assert defaults.snapshot() == before
    assert db.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0011_binding_parameter_defaults"}
    ]


@pytest.mark.parametrize(
    "database", [("postgresql", "0011_binding_parameter_defaults")], indirect=True
)
def test_downgrade_lock_before_writer_prevents_success_followed_by_history_loss(
    defaults: Defaults,
) -> None:
    db = defaults.bindings.db
    at_counts, release_counts = Event(), Event()

    def pause_before_counts() -> None:
        at_counts.set()
        assert release_counts.wait(5), "Test did not release the actual migration transaction"

    with ThreadPoolExecutor(max_workers=2) as workers:
        migration_result = workers.submit(
            downgrade_defaults,
            db,
            application="sn_defaults_first_" + db.schema,
            before_counts=pause_before_counts,
        )
        try:
            assert at_counts.wait(5)
            writer = workers.submit(defaults.port.create, defaults.prepare())
            db.await_waiter()
            assert not writer.done(), (
                "A writer must not succeed inside the downgrade check/DDL window"
            )
        finally:
            release_counts.set()
        migration_result.result(timeout=5)
        result = writer.result(timeout=5)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    assert all(not rows for rows in defaults.bindings.snapshot().values())
    assert db.query("SELECT to_regclass(%s) AS name", (TABLE,)) == [{"name": None}]
    assert db.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0010_model_binding_drafts"}
    ]


@pytest.mark.parametrize(
    "database", [("postgresql", "0011_binding_parameter_defaults")], indirect=True
)
@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_downgrade_refuses_old_transaction_snapshots_without_ddl(
    defaults: Defaults, isolation: str
) -> None:
    before = defaults.snapshot()
    with pytest.raises(RuntimeError, match="READ COMMITTED"):
        downgrade_defaults(
            defaults.bindings.db,
            application="sn_defaults_isolation",
            isolation=isolation,
        )
    assert defaults.snapshot() == before
    assert defaults.bindings.db.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0011_binding_parameter_defaults"}
    ]


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_failed_upgrade_rolls_back_new_column_constraint_and_acl(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0010_model_binding_drafts")
    prior = sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall()
    acl = sandbox.admin.execute(
        "SELECT relname,relacl FROM pg_class WHERE relnamespace=%s::regnamespace ORDER BY relname",
        (sandbox.schema,),
    ).fetchall()
    sandbox.admin.execute(f"CREATE TABLE {TABLE} (sentinel BIGINT)")
    with pytest.raises(ProgrammingError):
        sandbox.upgrade()
    assert sandbox.version() == "0010_model_binding_drafts"
    assert (
        sandbox.admin.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=%s "
            "AND table_name='speech_binding_commands' AND column_name='defaults_declared'",
            (sandbox.schema,),
        ).fetchall()
        == []
    )
    assert (
        sandbox.admin.execute(
            "SELECT conname FROM pg_constraint WHERE conname=%s AND connamespace=%s::regnamespace",
            (PARENT_KEY, sandbox.schema),
        ).fetchall()
        == []
    )
    assert sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall() == prior
    assert (
        sandbox.admin.execute(
            "SELECT relname,relacl FROM pg_class WHERE relnamespace=%s::regnamespace "
            "AND relname<>%s ORDER BY relname",
            (sandbox.schema, TABLE),
        ).fetchall()
        == acl
    )
