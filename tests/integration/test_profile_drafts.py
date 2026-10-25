"""Actual PG authority plus the same bounded SQL on minimum-role InnoDB."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

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
from tests.support.job_database import MySQLJobTransactions
from tests.support.profile_database import MySQLProfileDrafts, ProfileCursor
from tests.support.profile_examples import profile_request

from speechnexus_backend.speech.application.profile_drafts import ProfileDrafts
from speechnexus_backend.speech.domain.profile_drafts import (
    CreateProfileDraft,
    ProfileUnavailableError,
    document_digest,
)
from speechnexus_backend.speech.infrastructure import profile_drafts_sql as sql
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.profile_draft_schema import (
    PROFILE_DRAFT_DDL,
    profile_draft_grants,
)
from speechnexus_backend.speech.infrastructure.profile_drafts_postgres import (
    PostgresProfileDrafts,
    _Cursor,
)
from speechnexus_backend.speech.interfaces.profile_drafts import (
    draft_to_wire,
    prepare_profile_draft,
)

pytestmark = pytest.mark.integration
TABLES = ("speech_profile_commands", "speech_profile_families", "speech_profile_revisions")


@dataclass
class Profiles:
    db: JobDatabase
    port: ProfileDrafts
    mysql: MySQLProfileDrafts | None = None

    def prepare(self, key: str = "original-key", **changes: Any) -> CreateProfileDraft:
        return prepare_profile_draft({**profile_request(), **changes}, key)

    def snapshot(self) -> dict[str, Any]:
        return {t: self.db.query(f"SELECT * FROM {t} ORDER BY 1,2") for t in TABLES}

    @contextmanager
    def writer(self) -> Iterator[tuple[Any, Any]]:
        if self.mysql is not None:
            connection = self.mysql.connector.connect()
            try:
                with connection.cursor() as raw:
                    yield connection, ProfileCursor(raw)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
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
        pytest.fail("Actual profile database lock waits were not observed")


@pytest.fixture
def profiles(database: JobDatabase) -> Profiles:
    db = database
    if db.dialect == "postgresql":
        for grant in profile_draft_grants(db.schema, db.role):
            db.query(grant)
        assert db.pool is not None
        return Profiles(db, PostgresProfileDrafts(db.pool))
    for ddl in PROFILE_DRAFT_DDL:
        db.query(
            ddl.replace('COLLATE "C"', "COLLATE utf8mb4_0900_bin").replace("TEXT", "MEDIUMTEXT")
        )
    for table in TABLES:
        db.query(f"GRANT SELECT, INSERT ON `{db.schema}`.{table} TO '{db.role}'@'%'")
    db.query(
        f"GRANT UPDATE (latest_revision) ON `{db.schema}`.speech_profile_families "
        f"TO '{db.role}'@'%'"
    )
    db.query(
        f"GRANT UPDATE (command_state,revision,error_code) "
        f"ON `{db.schema}`.speech_profile_commands TO '{db.role}'@'%'"
    )
    mysql = MySQLProfileDrafts(
        MySQLJobTransactions(db.url, db.schema, db.role, "synthetic_job_test")
    )
    return Profiles(db, mysql, mysql)


def test_create_pinned_read_replay_and_rebuild_preserve_history(profiles: Profiles) -> None:
    command = profiles.prepare()
    first = profiles.port.create(command)
    assert first.outcome == "created" and first.draft is not None
    original = draft_to_wire(first.draft)
    second = profiles.port.create(
        profiles.prepare(
            "second-key",
            expected_latest_revision="1",
            display_name="Synthetic second revision",
        )
    )
    assert second.draft is not None and second.draft.revision == "2"
    assert original["publication"]["status"] == "draft" and "validation" not in original
    before = profiles.snapshot()
    replay = profiles.port.create(command)
    assert replay.outcome == "replayed" and replay.draft == first.draft
    assert profiles.port.recover(command).draft == first.draft
    rebuilt: ProfileDrafts = (
        MySQLProfileDrafts(profiles.mysql.connector)
        if profiles.mysql is not None
        else PostgresProfileDrafts(cast(SpeechPool, profiles.db.pool))
    )
    assert rebuilt.get(command.profile_id, "1") == first.draft
    assert rebuilt.get(command.profile_id, "999") is None
    assert profiles.snapshot() == before
    assert profiles.db.counts() == (0, 0), "Draft configuration cannot create a historical Job"


def test_original_key_conflicts_across_fields_and_families_without_reallocation(
    profiles: Profiles,
) -> None:
    original = profiles.prepare()
    assert profiles.port.create(original).outcome == "created"
    before = profiles.snapshot()
    for change in (
        {"display_name": "Changed"},
        {"profile_id": "other-family"},
        {"expected_latest_revision": "1"},
    ):
        changed = profiles.prepare(**change)
        for result in (profiles.port.create(changed), profiles.port.recover(changed)):
            assert result.outcome == "conflict" and result.error_code == "idempotency_conflict"
        assert profiles.snapshot() == before


def test_cas_rejection_is_durable_even_when_that_expected_revision_becomes_current(
    profiles: Profiles,
) -> None:
    rejected = profiles.prepare(expected_latest_revision="1")
    result = profiles.port.create(rejected)
    assert result.outcome == "rejected" and result.error_code == "configuration_changed"
    assert profiles.port.create(profiles.prepare("new-family-key")).outcome == "created"
    before = profiles.snapshot()
    assert profiles.port.create(rejected) == profiles.port.recover(rejected) == result
    assert profiles.snapshot() == before
    assert profiles.port.recover(profiles.prepare("never-submitted-key")).outcome == "unknown"
    assert profiles.snapshot() == before, "Recovery cannot insert even a missing command"


def test_same_key_blocks_on_original_transaction_then_replays_its_committed_revision(
    profiles: Profiles,
) -> None:
    command = profiles.prepare()
    with ThreadPoolExecutor(max_workers=1) as worker:
        with profiles.writer() as (_, cursor):
            original = sql.create_draft(cursor, command, cursor.clock)
            waiting = worker.submit(profiles.port.create, command)
            profiles.waiters(1)
            assert not waiting.done()
        replay = waiting.result(timeout=5)
    assert original.outcome == "created" and replay.outcome == "replayed"
    assert original.draft == replay.draft
    assert len(profiles.snapshot()["speech_profile_revisions"]) == 1


def test_different_keys_on_new_family_serialize_cas_and_persist_losing_result(
    profiles: Profiles,
) -> None:
    first, second = profiles.prepare("first-cas-key"), profiles.prepare("second-cas-key")
    profiles.db.query(
        "INSERT INTO speech_profile_families (profile_id) VALUES (%s)", (first.profile_id,)
    )
    blocker = profiles.db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT * FROM speech_profile_families FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as workers:
            left, right = (workers.submit(profiles.port.create, cmd) for cmd in (first, second))
            try:
                profiles.waiters(2)
                assert not left.done() and not right.done()
            finally:
                blocker.rollback()
            outcomes = [left.result(timeout=5), right.result(timeout=5)]
    finally:
        blocker.rollback()
        blocker.close()
    assert sorted(r.outcome for r in outcomes) == ["created", "rejected"]
    before = profiles.snapshot()
    for cmd, result in zip((first, second), outcomes, strict=True):
        recovered = profiles.port.recover(cmd)
        assert recovered.draft == result.draft and recovered.error_code == result.error_code
    assert profiles.snapshot() == before and len(before["speech_profile_revisions"]) == 1


@pytest.mark.parametrize("fail_after", ["speech_profile_families", "speech_profile_commands"])
def test_failures_after_actual_writes_roll_back_every_profile_projection(
    profiles: Profiles,
    monkeypatch: pytest.MonkeyPatch,
    fail_after: str,
) -> None:
    update = sql._update

    def fail(cursor: Any, statement: str, args: tuple[object, ...]) -> None:
        update(cursor, statement, args)
        if statement.startswith("UPDATE " + fail_after):
            raise ProfileUnavailableError

    monkeypatch.setattr(sql, "_update", fail)
    before = profiles.snapshot()
    command = profiles.prepare()
    assert profiles.port.create(command).recovery == "query_original"
    assert profiles.snapshot() == before
    assert profiles.port.recover(command).outcome == "unknown"
    assert profiles.snapshot() == before


def test_commit_succeeds_but_reply_lost_and_query_pressure_preserve_original_identity(
    profiles: Profiles,
) -> None:
    command = profiles.prepare()
    if profiles.mysql is not None:
        profiles.mysql.lose_reply = True
        result = profiles.port.create(command)
    else:

        def configure(connection: FaultConnection) -> None:
            connection.execute(f'SET search_path TO "{profiles.db.schema}",pg_catalog')
            connection.commit()

        with ConnectionPool[FaultConnection](
            make_conninfo(profiles.db.url, user=profiles.db.role, password="synthetic_job_test"),
            connection_class=FaultConnection,
            kwargs={"row_factory": dict_row},
            configure=configure,
            min_size=1,
            max_size=1,
        ) as pool:
            with pool.connection() as connection:
                armed = connection
            armed.fault = "after"
            result = PostgresProfileDrafts(cast(SpeechPool, pool)).create(command)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    before = profiles.snapshot()
    if profiles.mysql is not None:
        blocker = profiles.db.blocker()
        try:
            with blocker.cursor() as cursor:
                cursor.execute("LOCK TABLES speech_profile_commands WRITE")
            # Drive the actual oracle recovery with the same native MDL timeout.
            original_connect = profiles.mysql.connector.connect

            def pressured() -> Any:
                mysql_connection = original_connect()
                with mysql_connection.cursor() as cursor:
                    cursor.execute("SET SESSION lock_wait_timeout=1")
                return mysql_connection

            from unittest.mock import patch

            with patch.object(MySQLJobTransactions, "connect", lambda _: pressured()):
                unknown = profiles.port.recover(command)
            assert unknown.recovery == "query_original"
        finally:
            blocker.cursor().execute("UNLOCK TABLES")
            blocker.close()
    else:
        assert profiles.db.pool is not None
        # A separate one-slot pool makes acquisition pressure real and bounded.
        from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool

        with (
            speech_pool(
                make_conninfo(
                    profiles.db.url, user=profiles.db.role, password="synthetic_job_test"
                ),
                schema=profiles.db.schema,
                max_size=1,
            ) as pressure_pool,
            pressure_pool.connection(),
        ):
            assert (
                PostgresProfileDrafts(pressure_pool, timeout_seconds=0.05).recover(command).recovery
                == "query_original"
            )
    assert profiles.snapshot() == before
    restored = profiles.port.recover(command)
    assert restored.outcome == "replayed" and restored.draft is not None
    assert restored.draft.revision == "1" and profiles.snapshot() == before


def test_runtime_writer_cannot_rewrite_or_delete_immutable_history(profiles: Profiles) -> None:
    profiles.port.create(profiles.prepare())
    before = profiles.snapshot()
    for statement in (
        "UPDATE speech_profile_revisions SET definition_json='{}'",
        "UPDATE speech_profile_commands SET intent_json='{}'",
        "DELETE FROM speech_profile_commands",
        "DELETE FROM speech_profile_revisions",
        "DELETE FROM speech_profile_families",
    ):
        with pytest.raises((psycopg.Error, pymysql.Error)), profiles.writer() as (_, cursor):
            cursor.execute(statement)
    assert profiles.snapshot() == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_migration_repeat_and_refused_downgrade_preserve_profile_history(
    profiles: Profiles,
) -> None:
    profiles.port.create(profiles.prepare())
    before = profiles.snapshot()
    engine = create_engine(
        profiles.db.url.replace("postgresql://", "postgresql+psycopg://"), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = profiles.db.schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
            with pytest.raises(RuntimeError, match="Binding history downgrade"):
                migration.downgrade(config, "0008_runtime_delivery")
    finally:
        engine.dispose()
    assert profiles.snapshot() == before
    assert profiles.db.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0011_binding_parameter_defaults"}
    ]


def test_full_decimal_revision_range_and_exhaustion_do_not_overflow_or_allocate(
    profiles: Profiles,
) -> None:
    # A synthetic allocation anchor exercises the complete frozen 19-digit range.
    initial = profiles.prepare(expected_latest_revision="9999999999999999998")
    profiles.db.query(
        "INSERT INTO speech_profile_families (profile_id,latest_revision) VALUES (%s,%s)",
        (initial.profile_id, initial.expected_latest_revision),
    )
    created = profiles.port.create(initial)
    assert created.draft is not None and created.draft.revision == "9999999999999999999"
    assert profiles.port.get(initial.profile_id, created.draft.revision) == created.draft
    before = profiles.snapshot()
    exhausted = profiles.prepare("exhausted-key", expected_latest_revision=created.draft.revision)
    assert profiles.port.create(exhausted).recovery == "query_original"
    assert profiles.port.recover(exhausted).recovery == "query_original"
    assert profiles.snapshot() == before


def test_corrupt_deep_definition_fails_closed_without_changing_history(profiles: Profiles) -> None:
    command = profiles.prepare()
    profiles.port.create(command)
    raw = b"[" * 65535 + b"]" * 65535
    profiles.db.query(
        "UPDATE speech_profile_revisions SET definition_json=%s,definition_digest=%s",
        (raw.decode(), document_digest(raw)),
    )
    before = profiles.snapshot()
    assert profiles.port.recover(command).recovery == "query_original"
    with pytest.raises(ProfileUnavailableError):
        profiles.port.get(command.profile_id, "1")
    assert profiles.snapshot() == before
    assert (
        profiles.port.create(profiles.prepare("healthy-key", profile_id="healthy-family")).outcome
        == "created"
    )


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_failed_upgrade_rolls_back_new_tables_and_preserves_original_history(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0008_runtime_delivery")
    old = sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall()
    sandbox.admin.execute("CREATE TABLE speech_profile_commands (sentinel BIGINT)")
    with pytest.raises(ProgrammingError) as failure:
        sandbox.upgrade()
    assert getattr(failure.value.orig, "sqlstate", None) == "42P07"
    assert sandbox.version() == "0008_runtime_delivery"
    assert sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall() == old
    for table in ("speech_profile_families", "speech_profile_revisions"):
        assert (
            sandbox.admin.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()["name"]
            is None
        )
    sandbox.admin.execute("ALTER TABLE speech_profile_commands RENAME TO profile_collision_fixture")
    sandbox.upgrade("0009_profile_drafts")
    sandbox.upgrade("0009_profile_drafts")
    assert sandbox.version() == "0009_profile_drafts"
    with sandbox.engine.connect() as connection:
        sandbox.config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="Profile history downgrade"):
            migration.downgrade(sandbox.config, "0008_runtime_delivery")
    assert sandbox.version() == "0009_profile_drafts"
    assert sandbox.admin.execute("SELECT * FROM coordination_jobs").fetchall() == old
